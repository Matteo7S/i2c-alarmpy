#!/usr/bin/python
import os
import signal
import time
import threading
import traceback
import json
import bcrypt
from contextlib import closing
import smbio
import database
from config import Config


class AlarmManager:

    def __init__(self):
        self.alarm = Alarm()
        self._running = False
        self.COMMANDS = {
            "start": self.start_alarm,
            "stop": self.stop_alarm,
            "restart": self.restart_alarm,
            "action": self.action_alarm}

    def start_alarm(self, reason):
        self.alarm.start()
        self.log("Alarm thread start command sent: " + reason)

    def stop_alarm(self, reason):
        self.alarm.stop()
        self.log("Alarm thread stop command sent: " + reason)

    def restart_alarm(self, reason):
        self.alarm.restart()
        self.log("Alarm thread restart command sent: " + reason)

    def action_alarm(self, data):
        self.alarm.process_command(data["command"], data["reason"])
        self.log("Alarm thread action command sent")

    def log(self, message, error=None):
        timestamp = time.strftime("%Z %Y-%m-%d %H:%M:%S", time.localtime())
        if error:
            trace = traceback.format_exc()
            message += "\n" + trace
        message = timestamp + " " + message
        database.write_log(message, error=(error is not None))

    def catch_sigexit(self, sig, frame):
        self.log("Recivecd Exit signal: exiting ")
        self.stop_alarm("Recived Exit signal")
        self._running = False

    def catch_sigreload(self, sig, frame):
        self.log("Recivecd Reload signal: restarting ")
        self.restart_alarm("Recived Reload signal")

    def bind_sig(self):
        signal.signal(signal.SIGINT, self.catch_sigexit)
        signal.signal(signal.SIGTERM, self.catch_sigexit)
        signal.signal(signal.SIGHUP, self.catch_sigreload)

    def main(self):
        self._running = True
        self.bind_sig()
        if Config["auto_start"]:
            self.start_alarm("Auto Start")
            if Config["auto_arm"]:
                self.alarm.arm("Auto Arm")
        while self._running:
            try:
                with closing(database.get_db()) as db:
                    c = db.cursor()
                    c.execute("select cmd_id, data from cmdq;")
                    cmds = c.fetchall()
                    if cmds:
                        for cmd in cmds:
                            cmd_id, data_s = cmd
                            data = json.loads(data_s)
                            self.process_cmd(cmd_id, data)
                self.log_state()
                time.sleep(Config["manager_sleep"])
            except Exception as err:
                self.log("Error in manager Main loop", err)
            except KeyboardInterrupt:
                self.log("Recivecd Ctl-C: exiting ")
                self.stop_alarm("Recivecd Ctl-C")
                self._running = False
        self.log_state()

    def process_cmd(self, cmd_id, data):
        self.clear_cmd(cmd_id)
        if isinstance(data, dict):
            for key in data:
                if key in self.COMMANDS:
                    func = self.COMMANDS[key]
                    func(data[key])
        else:
            raise ValueError("Bad manager cmd data '%r'" % (data,))

    def clear_cmd(self, cmd_id):
        with closing(database.get_db()) as db:
            c = db.cursor()
            c.execute("delete from cmdq where cmd_id = %s" % (cmd_id,))
            db.commit()

    def log_state(self):
        with closing(database.get_db()) as db:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO state (key) VALUES ('alarm_thread');")
            c.execute(
                "UPDATE state SET "
                "data = :data, "
                "state_time = :time "
                "WHERE key = 'alarm_thread';",
                {
                    "data": json.dumps(self.alarm.is_running()),
                    "time": time.time()})

            db.commit()


class Alarm:

    DISARMED = 0
    ARMED = 1
    ARMDELAY = 2
    TRIPPED = 3
    ALARMED = 4
    FAULT = 5

    ALARM_STATES = {
        DISARMED: "Disarmed",
        ARMED: "Armed",
        ARMDELAY: "Arm-delay",
        TRIPPED: "Tripped",
        ALARMED: "Alarmed",
        FAULT: "Fault"}

    NOOP_STATES = [ARMDELAY, FAULT]

    ACTIONS = {
        "arm": None,
        "disarm": None,
        "trip": None,
        "alarm": None}

    def __init__(self):
        self.thread = None
        self._running = False
        self._configured = False
        self.ios = None
        self.interfaces = None
        self.MESSAGES = {
            "input": self.process_input,
            "switch": self.process_switch}

        self.ACTIONS["arm"] = self.arm
        self.ACTIONS["disarm"] = self.disarm
        self.ACTIONS["trip"] = self.trip
        self.ACTIONS["alarm"] = self.alarm

        self.state = 0
        self.last = time.time()
        self.armtime = 0

        self.buses = {}
        self.ios = {}
        self.interfaces = {}
        self.indicators = {}
        self.actions = {}

    def is_running(self):
        if self.thread is not None:
            return self.thread.is_alive()
        return False

    def arm(self, reason):
        if self.state in Alarm.NOOP_STATES:
            return
        if self.state == self.DISARMED:
            self.state = self.ARMDELAY
            self.armtime = time.time()
        self.log("ARM " + reason)

    def disarm(self, reason):
        if self.state in Alarm.NOOP_STATES:
            return
        self.state = self.DISARMED
        self.log("DISARM " + reason)

    def trip(self, reason):
        if self.state in Alarm.NOOP_STATES:
            return
        if self.state == Alarm.ARMED:
            self.state = Alarm.TRIPPED
            self.last = time.time()
            self.log("TRIPPED " + reason)
        elif self.state == Alarm.DISARMED:
            self.state = Alarm.FAULT
            self.log("FAULTED " + reason)
            self.last = time.time()

    def alarm(self, reason):
        self.state = Alarm.ALARMED
        self.log("ALARM " + reason, alarm=True)

    def update_state(self):
        for key in self.indicators:
            indicator = self.indicators[key]
            self.interfaces[indicator["interface"]].update_state(
                indicator["state"] == self.state)

    def update_tripped(self):
        now = time.time()
        if now - self.last > Config["tripped_timeout"]:
            self.alarm("Tripped timeout")

    def update_faulted(self):
        now = time.time()
        if now - self.last > Config["faulted_timeout"]:
            self.state = Alarm.DISARMED

    def update_armdelay(self):
        now = time.time()
        if now - self.armtime > Config["arm_delay"]:
            self.state = Alarm.ARMED

    def configure(self):
        with closing(database.get_db()) as db:
            c = db.cursor()
            self._configure_ios(c)
            self._configure_interfaces(c)
            self._configure_actions(c)
            self._configure_indicators(c)
            self._configured = True

    def _configure_ios(self, c):
            c.execute("select io_id, type, bus, addr from io;")
            ios = c.fetchall()
            if ios:
                self.ios = {}
                for io in ios:
                    io_id, t, bus, addr, = io
                    if t not in smbio.IOTYPES:
                        raise ValueError("invaid io type for io %s" % (io_id,))
                    if bus not in self.buses:
                        self.buses[bus] = smbio.smb.Bus(bus)
                    klass = smbio.IOMAP[smbio.IOTYPES[t]]
                    self.ios[io_id] = klass.create(self.buses[bus], addr)

    def _configure_interfaces(self, c):
            c.execute(
                "select interface_id, type, io_id, slot, data "
                "from interface;")
            interfaces = c.fetchall()
            if interfaces:
                self.interfaces = {}
                for interface in interfaces:
                    interface_id, t, io_id, slot, data_s = interface
                    if t not in smbio.INTERFACETYPES:
                        raise ValueError(
                            "invalid interface type for interface %s"
                            % (interface_id,))
                    data = json.loads(data_s)
                    klass = smbio.INTERFACEMAP[
                        smbio.INTERFACETYPES[t]]
                    self.interfaces[interface_id] = klass(
                        interface_id, self.ios[io_id][slot], data)

    def _configure_actions(self, c):
            c.execute(
                "select action_id, code_hash, command, reason "
                "from action;")
            actions = c.fetchall()
            if actions:
                self.actions = {}
                for action in actions:
                    action_id, code_hash, command, reason = action
                    self.actions[action_id] = {
                        "code_hash": code_hash,
                        "command": command,
                        "reason": reason}

    def _configure_indicators(self, c):
            c.execute(
                "select indicator_id, interface_id, state from indicator;")
            indicators = c.fetchall()
            if indicators:
                self.indicators = {}
                for indicator in indicators:
                    indicator_id, interface_id, state = indicator
                    self.indicators[indicator_id] = {
                        "interface": interface_id,
                        "state": state}

    def log(self, message, error=None, alarm=False):
        timestamp = time.strftime("%Z %Y-%m-%d %H:%M:%S", time.localtime())
        if error:
            trace = traceback.format_exc()
            message += "\n" + trace
        message = timestamp + " " + message
        database.write_log(message, error=(error is not None), alarm=alarm)

    def log_state(self, states):
        with closing(database.get_db()) as db:
            c = db.cursor()
            time_now = time.time()
            states["alarm"] = self.state
            for state, data in states.items():
                c.execute(
                    "INSERT OR IGNORE INTO state (key) VALUES (:key);",
                    {"key":  state})
                c.execute(
                    "UPDATE state SET "
                    "data = :data, "
                    "state_time = :time "
                    "WHERE key = :key;",
                    {
                        "key":  state,
                        "data": json.dumps(data),
                        "time": time_now})
            db.commit()

    def stop(self):
        if self._running:
            if self.thread is not None:
                self._running = False
                self.thread.join()
            if self.ios is not None:
                for io in self.ios.values():
                    io.reset()
            del self.thread
            del self.ios
            del self.interfaces
            self.thread = None
            self._configured = False
            self.ios = None
            self.interfaces = None

    def start(self):
        if not self._running:
            if self.thread is None:
                self.thread = threading.Thread(target=self.run)
                self._running = True
                self.configure()
                self.thread.start()

    def restart(self):
        self.stop()
        self.start()

    def run(self):
        try:
            if not self._configured:
                raise RuntimeError("Alarm system has no configuration")
            if self.ios is None:
                raise RuntimeError("No ios configured")
            if self.interfaces is None:
                raise RuntimeError("No interfaces configured")
            self.main()
        except Exception as err:
            self.log("Error running alarm main thread", error=err)
        self._running = False

    def update(self):
        if self.state == Alarm.ARMDELAY:
            self.update_armdelay()
        elif self.state == Alarm.TRIPPED:
            self.update_tripped()
        elif self.state == Alarm.FAULT:
            self.update_faulted()
        self.update_state()
        states = {}
        for key in self.interfaces:
            interface = self.interfaces[key]
            self.process_interface(interface, interface.update())
            self.process_messages(interface.pull_messages())
            states[key] = interface.get_state()
        self.log_state(states)

    def main(self):
        self.log("Alarm main loop starting")
        while self._running:
            self.update()
            time.sleep(Config["alarm_sleep"])
        self.log("Alarm main loop stoped")

    def process_interface(self, interface, message):
        for key in message:
            if key in self.MESSAGES:
                func = self.MESSAGES[key]
                func(message[key], interface)

    def process_messages(self, messages):
        for key in self.interfaces:
            interface = self.interfaces[key]
            interface.process_messages(messages)

    def process_command(self, cmd, reason):
        if cmd in self.ACTIONS:
            func = self.ACTIONS[cmd]
            func(reason)

    def process_input(self, data, interface):
        if data:
            self.log("got input from interface %s: %s" % (interface.pid, data))
            for key in self.actions:
                action = self.actions[key]
                code_hash = action["code_hash"]
                if bcrypt.hashpw(
                        data.encode('UTF-8'),
                        code_hash.encode('UTF-8')
                        ).decode('UTF-8') == code_hash:
                    self.process_command(
                        action["command"],
                        action["reason"])
                    break

    def process_switch(self, state, interface):
        if state:
            self.trip("Switch on interface {}: {} tripped".format(
                interface.pid, interface.desc))


def write_pid():
    with open(Config["pidfile"], "w") as f:
        f.write(str(os.getpid()))


def del_pid():
    os.remove(Config["pidfile"])

if __name__ == "__main__":
    database.init_db()
    a = AlarmManager()
    write_pid()
    a.main()
    del_pid()
