import shutil


class ConsoleBuffer:
    def __init__(self):
        pass


import ctypes
import ctypes.wintypes
import msvcrt
import sys


class Console:
    def __init__(self, debug=True):
        self.brush = Brush(self)
        self.debug = debug
        pass

    def size(self):
        terminal_size = shutil.get_terminal_size(fallback=(0, 0))
        self.debug_print(f'{terminal_size[0]}x{terminal_size[1]}')

    def debug_print(self, text):
        if self.debug:
            self.brush.print("debug:", text, fgcolor=14, bgcolor=4)

    # TODO: register for console size change

class WindowsConsole(Console):
    def __init__(self):
        super(WindowsConsole, self).__init__()
        self.kernel32 = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        self.setConsoleModeProto = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD
        )
        self.setConsoleModeParams = (1, "hConsoleHandle", 0), (1, "dwMode", 0)
        self.setConsoleMode = self.setConsoleModeProto(('SetConsoleMode', self.kernel32), self.setConsoleModeParams)

        self.getConsoleModeProto = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.LPDWORD
        )
        self.getConsoleModeParams = (1, "hConsoleHandle", 0), (1, "lpMode")
        self.getConsoleMode = self.getConsoleModeProto(('GetConsoleMode', self.kernel32), self.setConsoleModeParams)
        self.consoleHandleOut = msvcrt.get_osfhandle(sys.stdout.fileno())
        self.consoleHandleIn = msvcrt.get_osfhandle(sys.stdin.fileno())

    def GetConsoleMode(self, handle) -> int:
        dwMode = ctypes.wintypes.DWORD(0)
        lpMode = ctypes.wintypes.LPDWORD(dwMode)
        self.getConsoleMode(self.consoleHandleOut, lpMode)

        print(f' dwMode: {hex(dwMode.value)}')
        return dwMode.value

    def SetConsoleMode(self, handle, mode: int):
        dwMode = ctypes.wintypes.DWORD(mode)
        self.setConsoleMode(handle, dwMode)
        return

    def SetMode(self, handle, mask: int, enable: bool) -> bool:
        consoleMode = self.GetConsoleMode(handle)
        other_bits = mask ^ 0xFFFFFFFF
        expected_value = mask if enable else 0
        if (consoleMode & mask) == expected_value:
            return True

        consoleMode = (consoleMode & other_bits) | expected_value
        self.SetConsoleMode(handle, consoleMode)
        consoleMode = self.GetConsoleMode(handle)
        return (consoleMode & mask) == expected_value

    def EnableVT(self) -> bool:
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
        return self.SetMode(self.consoleHandleOut, ENABLE_VIRTUAL_TERMINAL_PROCESSING, True)

    def EnableWindowChangeSizeEvents(self):
        ENABLE_WINDOW_INPUT = 0x8
        return self.SetMode(self.consoleHandleIn, ENABLE_WINDOW_INPUT, True)


class Brush:
    def __init__(self, console=None):
        self.fgcolor = None
        self.bgcolor = None
        self.console = console

    RESET = '\x1B[0m'

    def FgColor(self, color):
        return f'\x1B[38;5;{color}m'

    def BgColor(self, color):
        return f'\x1B[48;5;{color}m'

    def print(self, *args, sep=' ', end='\n', file=None, fgcolor=None, bgcolor=None):
        if fgcolor is None and bgcolor is None:
            print(*args, sep=sep, end=end, file=file)
        else:
            color = (self.BgColor(bgcolor) if bgcolor else '') + (self.FgColor(fgcolor) if fgcolor else '')
            print(color+" ".join(map(str, args))+self.RESET, sep=sep, end=end, file=file)

    def SetFgColor(self, color):
        print(self.FgColor(color), end='')

    def SetBgColor(self, color):
        print(self.BgColor(color), end='')

    def Reset(self):
        print(self.RESET, end='')


class Test:
    @staticmethod
    def ColorLine(start, end, text='  ', use_color=False, width=2):
        for color in range(start, end):
            print(f'\x1B[48;5;{color}m{("{:"+ str(width) + "}").format(color) if use_color else text}', end='')
        print('\x1B[0m')

    @staticmethod
    def ColorLine24bit(start,end,step=0):
        for color in range(start, end, step):
            print(f'\x1B[48;2;{color};{color};{color}mXD', end='')
        print('\x1B[0m')



def test():
    wc = WindowsConsole()
    success = wc.EnableVT()
    print(f'EnableVT? {success}')
    if not success:
        print('Abort')
        return
    print("\x1B[34m" + 'TEST 8bit ANSII Codes' + "\x1B[0m")
    Test.ColorLine(0, 8, use_color=True, width=2)
    Test.ColorLine(8, 16, use_color=True, width=2)
    for red in range(0, 6):
        start = 16 + 6 * 6 * red
        end = start + 36
        Test.ColorLine(start, end, use_color=True, width=3)

    Test.ColorLine(232, 256, use_color=True, width=4)

    brush = Brush()

    brush.SetBgColor(4)
    brush.SetFgColor(14)
    print("TEST", end='')
    brush.Reset()
    print()

    brush.print("TEST", fgcolor=14, bgcolor=4)

    wc.size()
    #for i in range(0,255):
    #   Test.ColorLine24bit(16*i, 16*(i+1),1)


if __name__ == '__main__':
    test()
