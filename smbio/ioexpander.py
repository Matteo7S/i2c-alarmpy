import smbus
from .smb import IO, IOGroup


class MCP23008:
    '''
    call create to return a IOGroup with a IO
    linked to the 8 gpio pins of a MCP23008
    '''

    IODIR = 0x00  # Pin direction register
    GPIO = 0x09  # Register for inputs
    OLAT = 0x0A  # Register for outputs

    @classmethod
    def create(cls, bus, addr):
        if not isinstance(bus, smbus.SMBus):
            raise ValueError("bus must be a SMBus instance")
        return IOGroup(
            "MCP23008",
            bus,
            addr,
            [IO(bus, addr, cls.IODIR, cls.GPIO, cls.OLAT)]
        )


class MCP23017(IO):
    '''
    call create to return a IOGroup with two IOs
    linked to the 16 gpio pins of a MCP23017
    '''

    IODIRA = 0x00  # Pin direction register for A
    IODIRB = 0x01  # Pin direction register for B
    OLATA = 0x14  # Register for outputs on A
    OLATB = 0x15  # Register for outputs on B
    GPIOA = 0x12  # Register for inputs on A
    GPIOA = 0x13  # Register for inputs on B

    @classmethod
    def create(cls, bus, addr):
        if not isinstance(bus, smbus.SMBus):
            raise ValueError("bus must be a SMBus instance")
        return IOGroup(
            "MCP23017",
            bus,
            addr,
            [
                IO(bus, addr, cls.IODIRA, cls.GPIOA, cls.OLATA),
                IO(bus, addr, cls.IODIRB, cls.GPIOB, cls.OLATB)
            ]
        )
