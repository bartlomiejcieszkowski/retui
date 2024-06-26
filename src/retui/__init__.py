#!/usr/bin/env python3
__version__ = "0.0.1"

# Notes:
# You can have extra line of console, which won't be fully visible - as w/a just don't use last line
# If new size is greater, then fill with new lines, so we won't be drawing in the middle of screen
import asyncio
import ctypes
import ctypes.wintypes
import os

# TASK LIST:
# TODO: Percent handling inside Pane - guess will need to add start_x, start_y + width height taken from parent
# TODO: Redraw only when covered - blinking over ssh in tmux - temporary: redraw only on size change
# TODO: trim line to screen width on debug prints
# TODO: Widget Alignment other than TopLeft is broken, eg for BottomLeft, x,y should mean what? start at max y, x=0
# TODO: Relative dimensions, 1 Top 80 percent, 2nd bottom 20 percent - got 1 free line..
import selectors
import shutil
import signal
import sys
import threading
from abc import ABC, abstractmethod
from collections import deque
from enum import Enum, Flag, IntEnum, auto
from typing import List, Tuple, Union

from .base import Color, ColorBits, ConsoleColor, Point
from .defaults import default_value
from .enums import Alignment, DimensionsFlag, TextAlign, WordWrap
from .input_handling import VirtualKeyCodes
from .mapping import log_widgets
from .theme import Selectors


def is_windows() -> bool:
    return os.name == "nt"


if is_windows():
    import msvcrt
else:
    import fcntl
    import termios


class ConsoleBuffer:
    _buffer_cached = None

    def __init__(self, width, height, symbol, debug):
        self.width = width
        self.height = height
        self.symbol = symbol
        self.debug = debug
        self.buffer = []
        if self.debug:
            # print numbered border
            line = ""
            for col in range(width):
                line += str(col % 10)
            self.buffer.append(line)
            middle = symbol * (width - 2)
            for row in range(1, height - 1):
                self.buffer.append(str(row % 10) + middle + str(row % 10))
            self.buffer.append(line)
        else:
            line = symbol * width
            for i in range(height):
                self.buffer.append(line)

    def same(self, width, height, symbol, debug):
        return self.width == width and self.height == height and self.symbol == symbol and self.debug == debug

    @staticmethod
    def get_buffer(width, height, symbol=" ", debug=True):
        if ConsoleBuffer._buffer_cached and ConsoleBuffer._buffer_cached.same(width, height, symbol, debug):
            return ConsoleBuffer._buffer_cached.buffer

        ConsoleBuffer._buffer_cached = ConsoleBuffer(width, height, symbol, debug)
        return ConsoleBuffer._buffer_cached.buffer


def json_convert(key, value):
    if key == "alignment":
        if isinstance(value, Alignment):
            return value
        if value is None:
            value = "TopLeft"
        value = Alignment[value]
    elif key == "dimensions":
        if isinstance(value, DimensionsFlag):
            return value
        if value is None:
            value = "Absolute"
        value = DimensionsFlag[value]
    elif key == "text_align":
        if isinstance(value, TextAlign):
            return value
        if value is None:
            value = "TopLeft"
        value = TextAlign[value]
    elif key == "text_wrap":
        if isinstance(value, WordWrap):
            return value
        if value is None:
            value = "Wrap"
        value = WordWrap[value]
    return value


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.wintypes.SHORT), ("Y", ctypes.wintypes.SHORT)]


class KEY_EVENT_RECORD_Char(ctypes.Union):
    _fields_ = [
        ("UnicodeChar", ctypes.wintypes.WCHAR),
        ("AsciiChar", ctypes.wintypes.CHAR),
    ]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", ctypes.wintypes.BOOL),
        ("wRepeatCount", ctypes.wintypes.WORD),
        ("wVirtualKeyCode", ctypes.wintypes.WORD),
        ("wVirtualScanCode", ctypes.wintypes.WORD),
        ("uChar", KEY_EVENT_RECORD_Char),
        ("dwControlKeyState", ctypes.wintypes.DWORD),
    ]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", COORD),
        ("dwButtonState", ctypes.wintypes.DWORD),
        ("dwControlKeyState", ctypes.wintypes.DWORD),
        ("dwEventFlags", ctypes.wintypes.DWORD),
    ]


class INPUT_RECORD_Event(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
        ("MouseEvent", MOUSE_EVENT_RECORD),
        ("WindowBufferSizeEvent", COORD),
        ("MenuEvent", ctypes.c_uint),
        ("FocusEvent", ctypes.c_uint),
    ]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", ctypes.wintypes.WORD), ("Event", INPUT_RECORD_Event)]


class ConsoleEvent(ABC):
    def __init__(self):
        pass


class SizeChangeEvent(ConsoleEvent):
    def __init__(self):
        super().__init__()


class MouseEvent(ConsoleEvent):
    last_mask = 0xFFFFFFFF

    dwButtonState_to_Buttons = [[0, 0], [1, 2], [2, 1]]

    class Buttons(IntEnum):
        LMB = 0
        RMB = 2
        MIDDLE = 1
        WHEEL_UP = 64
        WHEEL_DOWN = 65

    class ControlKeys(Flag):
        LEFT_CTRL = 0x8

    def __init__(self, x, y, button: Buttons, pressed: bool, control_key_state, hover: bool):
        super().__init__()
        self.coordinates = (x, y)
        self.button = button
        self.pressed = pressed
        self.hover = hover
        # based on https://docs.microsoft.com/en-us/windows/console/mouse-event-record-str
        # but simplified - right ctrl => left ctrl
        self.control_key_state = control_key_state

    def __str__(self):
        return (
            f"MouseEvent x: {self.coordinates[0]} y: {self.coordinates[1]} button: {self.button} "
            f"pressed: {self.pressed} control_key: {self.control_key_state} hover: {self.hover}"
        )

    @classmethod
    def from_windows_event(cls, mouse_event_record: MOUSE_EVENT_RECORD):
        # on windows position is 0-based, top-left corner

        hover = False
        # zero indicates mouse button is pressed or released
        if mouse_event_record.dwEventFlags != 0:
            if mouse_event_record.dwEventFlags == 0x1:
                hover = True
            elif mouse_event_record.dwEventFlags == 0x4:
                # mouse wheel move, high word of dwButtonState is dir, positive up
                return cls(
                    mouse_event_record.dwMousePosition.X,
                    mouse_event_record.dwMousePosition.Y,
                    MouseEvent.Buttons(MouseEvent.Buttons.WHEEL_UP + ((mouse_event_record.dwButtonState >> 31) & 0x1)),
                    True,
                    None,
                    False,
                )
                # TODO: high word
            elif mouse_event_record.dwEventFlags == 0x8:
                # horizontal mouse wheel - NOT SUPPORTED
                return None
            elif mouse_event_record.dwEventFlags == 0x2:
                # double click - TODO: do we need this?
                return None

        ret_list = []

        # on Windows we get mask of pressed buttons
        # we can either pass mask around and worry about translating it outside
        # we will have two different handlers on windows and linux,
        # so we just translate it into serialized clicks
        changed_mask = mouse_event_record.dwButtonState ^ MouseEvent.last_mask
        if hover:
            changed_mask = mouse_event_record.dwButtonState

        if changed_mask == 0:
            return None

        MouseEvent.last_mask = mouse_event_record.dwButtonState

        for dwButtonState, button in MouseEvent.dwButtonState_to_Buttons:
            changed = changed_mask & (0x1 << dwButtonState)
            if changed:
                press = mouse_event_record.dwButtonState & (0x1 << dwButtonState) != 0

                event = cls(
                    mouse_event_record.dwMousePosition.X,
                    mouse_event_record.dwMousePosition.Y,
                    MouseEvent.Buttons(button),
                    press,
                    None,
                    hover,
                )
                ret_list.append(event)

        if len(ret_list) == 0:
            return None

        return ret_list

    @classmethod
    def from_sgr_csi(cls, button_hex: int, x: int, y: int, press: bool):
        # print(f"0x{button_hex:X}", file=sys.stderr)
        move_event = button_hex & 0x20
        if move_event:
            # OPT1: don't support move
            # return None
            # OPT2: support move like normal click
            button_hex = button_hex & (0xFFFFFFFF - 0x20)
            # FINAL: TODO: pass it as Move mouse event and let
            # button = None
            # 0x23 on simple move.. with M..
            # 0x20 on move with lmb
            # 0x22 on move with rmb
            # 0x21 on move with wheel
            if button_hex & 0xF == 0x3:
                return None

        # TODO: wheel_event = button_hex & 0x40
        ctrl_button = 0x8 if button_hex & 0x10 else 0x0

        # remove ctrl button
        button_hex = button_hex & (0xFFFFFFFF - 0x10)
        button = MouseEvent.Buttons(button_hex)
        # sgr - 1-based
        if y < 2:
            return None

        # 1-based - translate to 0-based
        return cls(x - 1, y - 1, button, press, ctrl_button, False)


class KeyEvent(ConsoleEvent):
    def __init__(
        self,
        key_down: bool,
        repeat_count: int,
        vk_code: int,
        vs_code: int,
        char,
        wchar,
        control_key_state,
    ):
        super().__init__()
        self.key_down = key_down
        self.repeat_count = repeat_count
        self.vk_code = vk_code
        self.vs_code = vs_code
        self.char = char
        self.wchar = wchar
        self.control_key_state = control_key_state

    def __str__(self):
        return (
            f"KeyEvent: vk_code={self.vk_code} vs_code={self.vs_code} char='{self.char}' wchar='{self.wchar}' "
            f"repeat={self.repeat_count} ctrl=0x{self.control_key_state:X} key_down={self.key_down} "
        )


class Event(Enum):
    MouseClick = auto()


class Rectangle:
    def __init__(self, column: int, row: int, width: int, height: int):
        self.column = column
        self.row = row
        self.width = width
        self.height = height

    def update(self, column: int, row: int, width: int, height: int):
        self.column = column
        self.row = row
        self.width = width
        self.height = height

    def update_tuple(self, dimensions: Union[Tuple[int, int, int, int], List]):
        self.column = dimensions[0]
        self.row = dimensions[1]
        self.width = dimensions[2]
        self.height = dimensions[3]

    def contains_point(self, column: int, row: int):
        return not (
            (self.row > row)
            or (self.row + self.height - 1 < row)
            or (self.column > column)
            or (self.column + self.width - 1 < column)
        )


class InputInterpreter:
    # linux
    # lmb 0, rmb 2, middle 1, wheel up 64 + 0, wheel down 64 + 1

    class State(Enum):
        Default = 0
        Escape = 1
        CSI_Bytes = 2

    # this class should
    # receive data
    # and parse it accordingly
    # if it is ESC then start parsing it as ansi escape code
    # and emit event once we parse whole sequence
    # otherwise pass it to... input handler?

    # better yet:
    # this class should provide read method, and wrap the input provided

    def __init__(self, readable_input):
        self.input = readable_input
        self.state = self.State.Default
        self.input_raw = []
        self.ansi_escape_sequence = []
        self.payload = deque()
        self.last_button_state = [0, 0, 0]
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.input, selectors.EVENT_READ)
        self.selector_timeout_s = 1.0
        self.read_count = 64

    # 9 Normal \x1B[ CbCxCy M , value + 32 -> ! is 1 - max 223 (255 - 32)
    # 1006 SGR  \x1B[<Pb;Px;Py[Mm] M - press m - release
    # 1015 URXVT \x1B[Pb;Px;Py M - not recommended, can be mistaken for DL
    # 1016 SGR-Pixel x,y are pixels instead of cells
    # https://invisible-island.net/xterm/ctlseqs/ctlseqs.html

    def parse(self):
        # ConsoleView.log(f'parse: {self.ansi_escape_sequence}')
        # should append to self.event_list
        length = len(self.ansi_escape_sequence)

        if length < 3:
            # minimal sequence is ESC [ (byte in range 0x40-0x7e)
            self.payload.append(str(self.ansi_escape_sequence))
            return

        # we can safely skip first 2 bytes
        if self.ansi_escape_sequence[2] == "<":
            # for mouse last byte will be m or M character
            if self.ansi_escape_sequence[-1] not in ("m", "M"):
                self.payload.append(str(self.ansi_escape_sequence))
                return
            # SGR
            idx = 0
            values = [0, 0, 0]
            temp_word = ""
            press = False
            for i in range(3, length + 1):
                ch = self.ansi_escape_sequence[i]
                if idx < 2:
                    if ch == ";":
                        values[idx] = int(temp_word, 10)
                        idx += 1
                        temp_word = ""
                        continue
                elif ch in ("m", "M"):
                    values[idx] = int(temp_word, 10)
                    if ch == "M":
                        press = True
                    break
                temp_word += ch
            # msft
            # lmb 0x1 rmb 0x2, lmb2 0x4 lmb3 0x8 lmb4 0x10
            # linux
            # lmb 0, rmb 2, middle 1, wheel up 64 + 0, wheel down 64 + 1
            # move 32 + key
            # shift   4
            # meta    8
            # control 16
            # print(f"0X{values[0]:X} 0X{values[1]:X} 0x{values[2]:X}, press={press}", file=sys.stderr)
            mouse_event = MouseEvent.from_sgr_csi(values[0], values[1], values[2], press)
            if mouse_event:
                self.payload.append(mouse_event)
            return

        # normal - TODO
        # self.payload.extend(str(self.ansi_escape_sequence))
        len_aes = len(self.ansi_escape_sequence)
        if len_aes == 3:
            third_char = ord(self.ansi_escape_sequence[2])
            vk_code = 0
            char = b"\x00"
            wchar = ""
            if third_char == 65:
                # A - Cursor Up
                vk_code = VirtualKeyCodes.VK_UP
            elif third_char == 66:
                # B - Cursor Down
                vk_code = VirtualKeyCodes.VK_DOWN
            elif third_char == 67:
                # C - Cursor Right
                vk_code = VirtualKeyCodes.VK_RIGHT
            elif third_char == 68:
                # D - Cursor Left
                vk_code = VirtualKeyCodes.VK_LEFT
            else:
                self.payload.append(str(self.input_raw))
                return
            self.payload.append(
                KeyEvent(
                    key_down=True,
                    repeat_count=1,
                    vk_code=vk_code,
                    vs_code=vk_code,
                    char=char,
                    wchar=wchar,
                    control_key_state=0,
                )
            )
        elif len_aes == 4:
            # 1 ~ Home
            # 2 ~ Insert
            # 3 ~ Delete
            # 4 ~ End
            # 5 ~ PageUp
            # 6 ~ PageDown
            pass
        elif len_aes == 5:
            # 1 1 ~ F1
            # ....
            # 1 5 ~ F5
            # 1 7 ~ F6
            # 1 8 ~ F7
            # 1 9 ~ F8
            # 2 0 ~ F9
            # 2 1 ~ F10
            # 2 3 ~ F11
            # 2 3 ~ F12
            pass

        self.payload.append(str(self.input_raw))
        return

    def parse_keyboard(self):
        if len(self.input_raw) > 1:
            # skip for now
            self.payload.append(str(self.input_raw))
            return
        wchar = self.input_raw[0]
        if wchar.isprintable() is False:
            # skip for now
            self.payload.append(str(self.input_raw))
            return
        # https://docs.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes
        # key a is for both upper and lower case
        # vk_code = wchar.lower()
        self.payload.append(
            KeyEvent(
                key_down=True,
                repeat_count=1,
                vk_code=VirtualKeyCodes.from_ascii(ord(wchar)),
                vs_code=ord(wchar),
                char=wchar.encode(),
                wchar=wchar,
                control_key_state=0,
            )
        )
        return

    def read(self, count: int = 1):
        # ESC [ followed by any number in range 0x30-0x3f, then any between 0x20-0x2f, and final byte 0x40-0x7e
        # TODO: this should be limited so if one pastes long, long text this wont create arbitrary size buffer
        ready = self.selector.select(self.selector_timeout_s)
        if not ready:
            return None

        ch = self.input.read(self.read_count)
        while ch is not None and len(ch) > 0:
            self.input_raw.extend(ch)
            ch = self.input.read(self.read_count)

        if len(self.input_raw) > 0:
            for i in range(0, len(self.input_raw)):
                ch = self.input_raw[i]
                if self.state != self.State.Default:
                    ord_ch = ord(ch)
                    if 0x20 <= ord_ch <= 0x7F:
                        if self.state == self.State.Escape:
                            if ch == "[":
                                self.ansi_escape_sequence.append(ch)
                                self.state = self.State.CSI_Bytes
                                continue
                        elif self.state == self.State.CSI_Bytes:
                            if 0x30 <= ord_ch <= 0x3F:
                                self.ansi_escape_sequence.append(ch)
                                continue
                            elif 0x40 <= ord_ch <= 0x7E:
                                # implicit IntermediateBytes
                                self.ansi_escape_sequence.append(ch)
                                self.parse()
                                self.state = self.State.Default
                                continue
                    # parse what we had collected so far, since we failed check above
                    self.parse()
                    self.state = self.State.Default
                    # intentionally fall through to regular parse
                # check if escape code
                if ch == "\x1B":
                    self.ansi_escape_sequence.clear()
                    self.ansi_escape_sequence.append(ch)
                    self.state = self.State.Escape
                    continue

                # pass input to handler
                # here goes key, but what about ctrl, shift etc.? these are regular AsciiChar equivalents
                # no key up, only key down, is there \x sequence to enable extended? should be imho
                self.parse_keyboard()
                pass
            # DEBUG - don't do "".join, as the sequences are not printable
            # self.payload.append(str(self.input_raw))
            self.input_raw.clear()

            if len(self.payload) > 0:
                payload = self.payload
                self.payload = deque()
                return payload

        return None


class ConsoleWidget(ABC):
    @classmethod
    def from_dict(cls, **kwargs):
        return cls(
            app=kwargs.pop("app"),
            identifier=kwargs.pop("id", None),
            x=kwargs.pop("x"),
            y=kwargs.pop("y"),
            width=kwargs.pop("width"),
            height=kwargs.pop("height"),
            alignment=json_convert("alignment", kwargs.pop("alignment", default_value("alignment"))),
            dimensions=json_convert("dimensions", kwargs.pop("dimensions", default_value("dimensions"))),
            tab_index=kwargs.pop("tab_index", default_value("tab_index")),
            scroll_horizontal=kwargs.pop("scroll_horizontal", default_value("scroll_horizontal")),
            scroll_vertical=kwargs.pop("scroll_vertical", default_value("scroll_vertical")),
        )

    def __init__(
        self,
        app,
        identifier: Union[str, None] = None,
        x: int = 0,
        y: int = 0,
        width: int = 0,
        height: int = 0,
        alignment: Alignment = default_value("alignment"),
        dimensions: DimensionsFlag = default_value("dimensions"),
        tab_index: int = default_value("tab_index"),
        scroll_horizontal: bool = default_value("scroll_horizontal"),
        scroll_vertical: bool = default_value("scroll_vertical"),
    ):
        if identifier is None:
            identifier = f"{type(self).__qualname__}_{hash(self):x}"
        # TODO: check if it is unique
        self.identifier = identifier
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.alignment = alignment
        self.app = app
        self.dimensions = dimensions
        self.parent = None
        self.handlers = {}
        self.tab_index = tab_index
        self.scroll_horizontal = scroll_horizontal
        self.scroll_vertical = scroll_vertical
        # register handlers here
        # when handling click - cache what was there to speed up lookup - invalidate on re-draw
        # iterate in reverse order on widgets - the order on widget list determines Z order
        # - higher idx covers lower one
        self.last_dimensions = Rectangle(0, 0, 0, 0)

        # internals
        self._redraw = True
        self._update_size = True

    def update_dimensions(self):
        self._update_size = False
        # update dimensions is separate, so we separate drawing logic, so if one implement own widget
        # doesn't have to remember to call update_dimensions every time or do it incorrectly
        x = self.x
        y = self.y
        width = self.width_calculated()
        height = self.height_calculated()
        if Alignment.Float in self.alignment:
            # FIXME here be dragons
            pass
        else:
            if Alignment.Left in self.alignment:
                #  x
                #   []
                #  0 1 2
                x += self.parent.inner_x()
                pass
            elif Alignment.Right in self.alignment:
                #      x
                #   []
                #  2 1 0
                x = self.parent.inner_x() + self.parent.inner_width() - width - x
                pass
            if Alignment.Top in self.alignment:
                #  y   0
                #   [] 1
                #      2
                y += self.parent.inner_y()
                pass
            elif Alignment.Bottom in self.alignment:
                #      2
                #   [] 0
                #  y   1
                y = self.parent.inner_y() + self.parent.inner_height() - height - y
                pass

        self.last_dimensions.update(x, y, width, height)
        self._redraw = True

    def get_widget(self, column: int, row: int) -> Union["ConsoleWidget", None]:
        return self if self.contains_point(column, row) else None

    def get_widget_by_id(self, identifier: str) -> Union["ConsoleWidget", None]:
        return self if self.identifier == identifier else None

    def handle(self, event):
        # guess we should pass also unknown args
        # raise Exception('handle')
        pass

    def draw(self, force: bool = False):
        self._redraw = False

    def width_calculated(self):
        if DimensionsFlag.RelativeWidth in self.dimensions:
            return (self.width * self.parent.inner_width()) // 100
        elif DimensionsFlag.FillWidth in self.dimensions:
            # TODO: this should be width left
            return self.parent.inner_width()
        else:
            return self.width

    def height_calculated(self):
        if DimensionsFlag.RelativeHeight in self.dimensions:
            # concern about rows - 1
            return (self.height * self.parent.inner_height()) // 100
        elif DimensionsFlag.FillHeight in self.dimensions:
            # TODO: this should be height left
            return self.parent.inner_height()
        else:
            return self.height

    def contains_point(self, column: int, row: int):
        return self.last_dimensions.contains_point(column, row)

    def __str__(self):
        return (
            f"[x:{self.x} y:{self.x} width:{self.width} height:{self.height}"
            f"alignment:{self.alignment} dimensions:{self.dimensions} type:{type(self)} 0x{hash(self):X}]"
        )


class Console:
    @abstractmethod
    def get_brush(self):
        pass

    def __init__(self, app, debug=True):
        # TODO: this would print without vt enabled yet update state if vt enabled in brush?
        self.app = app
        self.columns, self.rows = self.get_size()
        self.vt_supported = False
        self.debug = debug
        pass

    def update_size(self):
        self.columns, self.rows = self.get_size()
        return self.columns, self.rows

    @staticmethod
    def get_size():
        columns, rows = shutil.get_terminal_size(fallback=(0, 0))
        # You can't use all lines, as it would move terminal 1 line down
        rows -= 1
        # OPEN: argparse does -2 for width
        # self.debug_print(f'{columns}x{rows}')
        return columns, rows

    def set_color_mode(self, enable: bool) -> bool:
        # TODO: careful with overriding
        self.vt_supported = enable
        return enable

    @abstractmethod
    def interactive_mode(self):
        pass

    @abstractmethod
    def read_events(self, callback, callback_ctx) -> bool:
        pass

    def set_title(self, title):
        if self.vt_supported:
            print(f"\033]2;{title}\007")


class LinuxConsole(Console):
    def get_brush(self):
        return Brush(self.vt_supported)

    def __init__(self, app):
        super().__init__(app)
        self.is_interactive_mode = False
        self.window_changed = False
        self.prev_fl = fcntl.fcntl(sys.stdin, fcntl.F_GETFL)
        new_fl = self.prev_fl | os.O_NONBLOCK
        fcntl.fcntl(sys.stdin, fcntl.F_SETFL, new_fl)
        self.app.log(f"stdin fl: 0x{self.prev_fl:X} -> 0x{new_fl:X}")
        self.prev_tc = termios.tcgetattr(sys.stdin)
        new_tc = termios.tcgetattr(sys.stdin)
        # manipulating lflag
        new_tc[3] = new_tc[3] & ~termios.ECHO  # disable input echo
        new_tc[3] = new_tc[3] & ~termios.ICANON  # disable canonical mode - input available immediately
        # cc
        # VMIN | VTIME | Result
        # =0   | =0    | non-blocking read
        # =0   | >0    | timed read
        # >0   | >0    | timer started on 1st char read
        # >0   | =0    | counted read
        new_tc[6][termios.VMIN] = 0
        new_tc[6][termios.VTIME] = 0
        termios.tcsetattr(sys.stdin, termios.TCSANOW, new_tc)  # TCSADRAIN?
        self.app.log(f"stdin lflags: 0x{self.prev_tc[3]:X} -> 0x{new_tc[3]:X}")
        self.app.log(f"stdin cc VMIN: 0x{self.prev_tc[6][termios.VMIN]} -> 0x{new_tc[6][termios.VMIN]}")
        self.app.log(f"stdin cc VTIME: 0x{self.prev_tc[6][termios.VTIME]} -> 0x{new_tc[6][termios.VTIME]}")

        self.input_interpreter = InputInterpreter(sys.stdin)

    def __del__(self):
        # restore stdin
        if self.is_interactive_mode:
            print("\x1B[?10001")

        termios.tcsetattr(sys.stdin, termios.TCSANOW, self.prev_tc)
        fcntl.fcntl(sys.stdin, fcntl.F_SETFL, self.prev_fl)
        print("xRestore console done")
        if self.is_interactive_mode:
            print("\x1B[?1006l\x1B[?1015l\x1B[?1003l")
        # where show cursor?

    window_change_event_ctx = None

    @staticmethod
    def window_change_handler(signum, frame):
        LinuxConsole.window_change_event_ctx.window_change_event()

    def window_change_event(self):
        # inject special input on stdin?
        self.window_changed = True

    def interactive_mode(self):
        self.is_interactive_mode = True
        LinuxConsole.window_change_event_ctx = self
        signal.signal(signal.SIGWINCH, LinuxConsole.window_change_handler)
        # ctrl-z not allowed
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)
        # enable mouse - xterm, sgr1006
        print("\x1B[?1003h\x1B[?1006h")
        # focus event
        # CSI I on focus
        # CSI O on loss
        print("\x1B[?1004h")

    def read_events(self, callback, callback_ctx) -> bool:
        events_list = []

        if self.window_changed:
            self.window_changed = False
            events_list.append(SizeChangeEvent())
        else:
            ret = self.input_interpreter.read()
            if ret:
                # passing around deque..
                events_list.append(ret)

        if len(events_list):
            callback(callback_ctx, events_list)
        return True


def no_print(fmt, *args):
    pass


def demo_fun(app):
    app.demo_event.wait(app.demo_time_s)
    print(f"DEMO MODE - {app.demo_time_s}s - END")
    if app.demo_event.is_set():
        return
    app.running = False


class App:
    log = no_print

    def __init__(self, log=no_print, title=None, debug: bool = False):
        App.log = log
        self.title = title
        self.log = log

        if is_windows():
            self.console = WindowsConsole(self)
        else:
            self.console = LinuxConsole(self)
        self.widgets = []
        self.brush = self.console.get_brush()
        self.debug_colors = ConsoleColor(None, None)
        self.running = False

        self.width = 0
        self.height = 0
        self.handle_sigint = True

        self.mouse_lmb_state = 0

        self.column_row_widget_cache = {}

        self.demo_thread = None
        self.demo_time_s = None
        self.demo_event = None
        self.emulate_screen_dimensions = None
        self.debug = debug

        self._redraw = True
        self._update_size = True

        # Scrollable attributes
        self.scroll_horizontal = False
        self.scroll_vertical = False

    def inner_x(self):
        return 0

    def inner_y(self):
        return 0

    def inner_width(self):
        return self.width

    def inner_height(self):
        return self.height

    def debug_print(self, text, end="\n", row_off=-1):
        if self.debug and self.log:
            self.log(text)
        elif True:  # self.debug:
            row = (0 if row_off >= 0 else self.console.rows) + row_off
            self.brush.move_cursor(row=row)
            print(self.debug_colors)
            self.brush.print(text, end=end, color=self.debug_colors)

    def clear(self, reuse=True):
        self.width, self.height = self.console.update_size()
        if reuse:
            self.brush.move_cursor(0, 0)
        for line in ConsoleBuffer.get_buffer(self.console.columns, self.console.rows, " ", debug=False):
            print(line, end="\n", flush=True)
        self._update_size = True

    def get_widget(self, column: int, row: int) -> Union[ConsoleWidget, None]:
        for idx in range(len(self.widgets) - 1, -1, -1):
            widget = self.widgets[idx].get_widget(column, row)
            if widget:
                return widget
        return None

    def get_widget_by_id(self, identifier) -> Union[ConsoleWidget, None]:
        for idx in range(0, len(self.widgets)):
            widget = self.widgets[idx].get_widget_by_id(identifier)
            if widget:
                return widget
        return None

    def handle_click(self, event: MouseEvent):
        # naive cache - based on clicked point
        # pro - we can create heat map
        # cons - it would be better with rectangle
        widget = self.column_row_widget_cache.get(event.coordinates, 1)
        if isinstance(widget, int):
            widget = self.get_widget(event.coordinates[0], event.coordinates[1])
            self.column_row_widget_cache[event.coordinates] = widget
        if widget:
            widget.handle(event)

        return widget

    @staticmethod
    def handle_events_callback(ctx, events_list):
        ctx.handle_events(events_list)

    def handle_events(self, events_list):
        off = -2
        col = 0
        # with -1 - 2 lines nearest end of screen overwrite each other
        for event in events_list:
            if isinstance(event, deque):
                self.handle_events(event)
            elif isinstance(event, list):
                self.handle_events(event)
            elif isinstance(event, MouseEvent):
                # we could use mask here, but then we will handle holding right button and
                # pressing/releasing left button and other combinations and frankly I don't want to
                # if (event.button_state & 0x1) == 0x1 and event.event_flags == 0:
                # widget = None
                # if event.button == event.button.LMB:
                #    widget = self.handle_click(event)
                # elif event.button == event.button.RMB:
                #    widget = self.handle_click(event)
                widget = self.handle_click(event)

                self.brush.move_cursor(row=(self.console.rows + off) - 1)
                if widget:
                    self.log(
                        f"x: {event.coordinates[0]} y: {event.coordinates[1]} "
                        f"button:{event.button} press:{event.pressed} widget:{widget}"
                    )

                self.debug_print(event, row_off=-4)
            elif isinstance(event, SizeChangeEvent):
                self.clear()
                self.debug_print(f"size: {self.console.columns:3}x{self.console.rows:3}", row_off=-2)
            elif isinstance(event, KeyEvent):
                self.debug_print(event, row_off=-3)
            else:
                self.brush.move_cursor(row=(self.console.rows + off) - 0, column=col)
                debug_string = f'type={type(event)} event="{event}", '
                # col = len(debug_string)
                self.debug_print(debug_string, row_off=-1)
                pass

    signal_sigint_ctx = None

    @staticmethod
    def signal_sigint_handler(signum, frame):
        App.signal_sigint_ctx.signal_sigint()

    def signal_sigint(self):
        self.running = False
        # TODO: read_events is blocking, sos this one needs to be somehow inject, otherwise we wait for first new event
        # works accidentally - as releasing ctrl-c cause key event ;)

    def demo_mode(self, time_s):
        self.demo_time_s = time_s

    def emulate_screen(self, height: int, width: int):
        self.emulate_screen_dimensions = (height, width)

    def draw(self, force: bool = False):
        if force or self._redraw:
            for widget in self.widgets:
                widget.draw(force=force)
            self._redraw = False
        self.brush.move_cursor(row=self.console.rows - 1)

    def update_dimensions(self):
        self._update_size = False
        for widget in self.widgets:
            widget.update_dimensions()
        self._redraw = True

    def run(self) -> int:
        if self.running is True:
            return -1

        if self.debug:
            log_widgets(self.log)

        if self.emulate_screen_dimensions:
            self.console.rows = self.emulate_screen_dimensions[0]
            self.console.columns = self.emulate_screen_dimensions[1]

        if self.title:
            self.console.set_title(self.title)

        if self.handle_sigint:
            App.signal_sigint_ctx = self
            signal.signal(signal.SIGINT, App.signal_sigint_handler)

        if self.demo_time_s and self.demo_time_s > 0:
            self.demo_event = threading.Event()
            self.demo_thread = threading.Thread(target=demo_fun, args=(self,))
            self.demo_thread.start()
            if isinstance(self.console, WindowsConsole):
                self.console.blocking_input(False)

        self.running = True

        # create blank canvas
        self.clear(reuse=False)

        self.console.interactive_mode()

        self.brush.cursor_hide()
        self.handle_events([SizeChangeEvent()])

        self.register_tasks()

        asyncio.run(self.main_loop())

        if self.demo_thread and self.demo_thread.is_alive():
            self.demo_event.set()
            self.demo_thread.join()

        # Move to the end, so we won't end up writing in middle of screen
        self.brush.move_cursor(self.console.rows - 1)
        self.brush.cursor_show()
        return 0

    async def main_loop(self):
        while self.running:
            if self._update_size:
                self.column_row_widget_cache.clear()
                self.update_dimensions()
            self.draw()

            # this is blocking
            if not self.console.read_events(self.handle_events_callback, self):
                break

    def color_mode(self, enable=True) -> bool:
        if enable:
            success = self.console.set_color_mode(enable)
            self.brush.color_mode(success)
            if success:
                # self.brush.color_mode(enable)
                self.debug_colors = ConsoleColor(Color(14, ColorBits.Bit8), Color(4, ColorBits.Bit8))
        else:
            self.debug_colors = ConsoleColor(None, None)
            self.brush.color_mode(enable)
            success = self.console.set_color_mode(enable)
        return success

    def add_widget(self, widget: ConsoleWidget) -> None:
        widget.parent = self
        self.widgets.append(widget)

    def add_widget_after(self, widget: ConsoleWidget, widget_on_list: ConsoleWidget) -> bool:
        try:
            idx = self.widgets.index(widget_on_list)
        except ValueError:
            return False

        widget.parent = self
        self.widgets.insert(idx + 1, widget)
        return True

    def add_widget_before(self, widget: ConsoleWidget, widget_on_list: ConsoleWidget) -> bool:
        try:
            idx = self.widgets.index(widget_on_list)
        except ValueError:
            return False

        widget.parent = self
        self.widgets.insert(idx, widget)
        return True


class WindowsConsole(Console):
    def get_brush(self):
        return Brush(self.vt_supported)

    def __init__(self, app):
        super().__init__(app)
        self.kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        set_console_mode_proto = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD)
        set_console_mode_params = (1, "hConsoleHandle", 0), (1, "dwMode", 0)
        self.setConsoleMode = set_console_mode_proto(("SetConsoleMode", self.kernel32), set_console_mode_params)

        get_console_mode_proto = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL, ctypes.wintypes.HANDLE, ctypes.wintypes.LPDWORD
        )
        get_console_mode_params = (1, "hConsoleHandle", 0), (1, "lpMode", 0)
        self.getConsoleMode = get_console_mode_proto(("GetConsoleMode", self.kernel32), get_console_mode_params)
        self.consoleHandleOut = msvcrt.get_osfhandle(sys.stdout.fileno())
        self.consoleHandleIn = msvcrt.get_osfhandle(sys.stdin.fileno())

        read_console_input_proto = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.LPVOID,  # PINPUT_RECORD
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPDWORD,
        )
        read_console_input_params = (
            (1, "hConsoleInput", 0),
            (1, "lpBuffer", 0),
            (1, "nLength", 0),
            (1, "lpNumberOfEventsRead", 0),
        )
        self.readConsoleInput = read_console_input_proto(
            ("ReadConsoleInputW", self.kernel32), read_console_input_params
        )

        get_number_of_console_input_events_proto = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL, ctypes.wintypes.HANDLE, ctypes.wintypes.LPDWORD
        )
        get_number_of_console_input_events_params = (1, "hConsoleInput", 0), (
            1,
            "lpcNumberOfEvents",
            0,
        )
        self.getNumberOfConsoleInputEvents = get_number_of_console_input_events_proto(
            ("GetNumberOfConsoleInputEvents", self.kernel32),
            get_number_of_console_input_events_params,
        )
        self.blocking = True

    KEY_EVENT = 0x1
    MOUSE_EVENT = 0x2
    WINDOW_BUFFER_SIZE_EVENT = 0x4

    def interactive_mode(self):
        self.SetWindowChangeSizeEvents(True)
        self.SetMouseInput(True)

    def blocking_input(self, blocking: bool):
        self.blocking = blocking

    def read_console_input(self):
        record = INPUT_RECORD()
        number_of_events = ctypes.wintypes.DWORD(0)
        if self.blocking is False:
            ret_val = self.getNumberOfConsoleInputEvents(self.consoleHandleIn, ctypes.byref(number_of_events))
            if number_of_events.value == 0:
                return None

        ret_val = self.readConsoleInput(
            self.consoleHandleIn,
            ctypes.byref(record),
            1,
            ctypes.byref(number_of_events),
        )
        if ret_val == 0:
            return None
        return record

    def read_events(self, callback, callback_ctx) -> bool:
        events_list = []
        # TODO: N events
        record = self.read_console_input()
        if record is None:
            pass
        elif record.EventType == self.WINDOW_BUFFER_SIZE_EVENT:
            events_list.append(SizeChangeEvent())
        elif record.EventType == self.MOUSE_EVENT:
            event = MouseEvent.from_windows_event(record.Event.MouseEvent)
            if event:
                events_list.append(event)
        elif record.EventType == self.KEY_EVENT:
            events_list.append(
                KeyEvent(
                    key_down=bool(record.Event.KeyEvent.bKeyDown),
                    repeat_count=record.Event.KeyEvent.wRepeatCount,
                    vk_code=record.Event.KeyEvent.wVirtualKeyCode,
                    vs_code=record.Event.KeyEvent.wVirtualScanCode,
                    char=record.Event.KeyEvent.uChar.AsciiChar,
                    wchar=record.Event.KeyEvent.uChar.UnicodeChar,
                    control_key_state=record.Event.KeyEvent.dwControlKeyState,
                )
            )
        else:
            pass

        if len(events_list):
            callback(callback_ctx, events_list)
        return True

    def GetConsoleMode(self, handle) -> int:
        dwMode = ctypes.wintypes.DWORD(0)
        # lpMode = ctypes.wintypes.LPDWORD(dwMode)
        # don't create pointer if not going to use it in python, use byref
        self.getConsoleMode(handle, ctypes.byref(dwMode))

        # print(f' dwMode: {hex(dwMode.value)}')
        return dwMode.value

    def SetConsoleMode(self, handle, mode: int):
        dwMode = ctypes.wintypes.DWORD(mode)
        self.setConsoleMode(handle, dwMode)
        return

    def SetMode(self, handle, mask: int, enable: bool) -> bool:
        console_mode = self.GetConsoleMode(handle)
        other_bits = mask ^ 0xFFFFFFFF
        expected_value = mask if enable else 0
        if (console_mode & mask) == expected_value:
            return True

        console_mode = (console_mode & other_bits) | expected_value
        self.SetConsoleMode(handle, console_mode)
        console_mode = self.GetConsoleMode(handle)
        return (console_mode & mask) == expected_value

    def SetVirtualTerminalProcessing(self, enable: bool) -> bool:
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
        return self.SetMode(self.consoleHandleOut, ENABLE_VIRTUAL_TERMINAL_PROCESSING, enable)

    def set_color_mode(self, enable: bool) -> bool:
        success = self.SetVirtualTerminalProcessing(enable)
        return super().set_color_mode(enable & success)

    def SetWindowChangeSizeEvents(self, enable: bool) -> bool:
        ENABLE_WINDOW_INPUT = 0x8
        return self.SetMode(self.consoleHandleIn, ENABLE_WINDOW_INPUT, enable)

    def SetQuickEditMode(self, enable: bool) -> bool:
        ENABLE_QUICK_EDIT_MODE = 0x40
        return self.SetMode(self.consoleHandleIn, ENABLE_QUICK_EDIT_MODE, enable)

    def SetMouseInput(self, enable: bool) -> bool:
        # Quick Edit Mode blocks mouse events
        self.SetQuickEditMode(False)
        ENABLE_MOUSE_INPUT = 0x10
        return self.SetMode(self.consoleHandleIn, ENABLE_MOUSE_INPUT, enable)


class Theme:
    class Colors:
        def __init__(self):
            self.text = ConsoleColor(Color(0, ColorBits.Bit24))

        @classmethod
        def monokai(cls):
            # cyan = 0x00B9D7
            # gold_brown = 0xABAA98
            # green = 0x82CDB9
            # off_white = 0xF5F5F5
            # orange = 0xF37259
            # pink = 0xFF3D70
            # pink_magenta = 0xF7208B
            # yellow = 0xF9F5C2
            pass

    def __init__(self, border: list[Point]):
        # border string
        # 155552
        # 600007
        # 600007
        # 388884
        # where the string is in form
        # '012345678'

        # validate border
        self.border = []
        if len(border) >= 9:
            for i in range(0, 9):
                if not isinstance(border[i], Point):
                    break
                self.border.append(border[i])

        if len(self.border) < 9:
            # invalid border TODO
            self.border = 9 * [Point(" ")]

        self.selectors = Selectors()

    def border_set_color(self, color):
        for i in range(1, 9):
            self.border[i].color = color

    def border_inside_set_color(self, color):
        self.border[0].color = color

    @staticmethod
    def border_from_str(border_str: str) -> list[Point]:
        border = []
        if len(border_str) < 9:
            raise Exception(f"border_str must have at least len of 9 - got {len(border_str)}")
        for i in range(0, 9):
            border.append(Point(border_str[i]))
        return border

    @classmethod
    def default_theme(cls):
        border = [
            Point(" "),
            Point("+"),
            Point("+"),
            Point("+"),
            Point("+"),
            Point("-"),
            Point("|"),
            Point("|"),
            Point("-"),
        ]
        return cls(border=border)

    @classmethod
    def other_theme(cls):
        border = [
            Point(" "),
            Point(" "),
            Point(" "),
            Point("|"),
            Point("|"),
            Point("_"),
            Point("|"),
            Point("|"),
            Point("_"),
        ]
        return cls(border=border)

    @classmethod
    def double_line_theme(cls):
        border = [
            Point(" "),
            Point("╔"),
            Point("╗"),
            Point("╚"),
            Point("╝"),
            Point("═"),
            Point("║"),
            Point("║"),
            Point("═"),
        ]
        return cls(border=border)

    @classmethod
    def single_line_light_theme(cls):
        border = [
            Point(" "),
            Point("┌"),
            Point("┐"),
            Point("└"),
            Point("┘"),
            Point("─"),
            Point("│"),
            Point("│"),
            Point("─"),
        ]
        return cls(border=border)

    @classmethod
    def single_line_heavy_theme(cls):
        border = [
            Point(" "),
            Point("┏"),
            Point("┓"),
            Point("┗"),
            Point("┛"),
            Point("━"),
            Point("┃"),
            Point("┃"),
            Point("━"),
        ]
        return cls(border=border)

    @classmethod
    def single_line_heavy_top_light_rest_theme(cls):
        border = [
            Point(" "),
            Point("┍"),
            Point("┑"),
            Point("└"),
            Point("┘"),
            Point("━"),
            Point("│"),
            Point("│"),
            Point("─"),
        ]
        return cls(border=border)

    @classmethod
    def single_line_light_rounded_corners_theme(cls):
        # unciode chars box drawing https://www.w3.org/TR/xml-entity-names/025.html
        border = [
            Point(" "),
            Point("╭"),
            Point("╮"),
            Point("╰"),
            Point("╯"),
            Point("─"),
            Point("│"),
            Point("│"),
            Point("─"),
        ]
        return cls(border=border)


APP_THEME = Theme.default_theme()


class Brush:
    def __init__(self, use_color=True):
        self.file = sys.stdout
        self.console_color = ConsoleColor()
        self.use_color = use_color
        # TODO: this comes from vt_supported, we override it with color_mode

    RESET = "\x1B[0m"

    def color_mode(self, enable=True):
        self.use_color = enable

    def foreground_color(self, color: Color, check_last=False):
        updated = self.console_color.update_foreground(color)
        if (not updated and check_last) or (self.console_color.foreground is None):
            return ""
        return f"\x1B[38;{int(self.console_color.foreground.bits)};{self.console_color.foreground.color}m"

    def background_color(self, color: Color, check_last=False):
        updated = self.console_color.update_background(color)
        if (not updated and check_last) or (self.console_color.background is None):
            return ""
        return f"\x1B[48;{int(self.console_color.background.bits)};{self.console_color.background.color}m"

    def color(self, console_color: ConsoleColor, check_last=False):
        if self.console_color == console_color:
            return ""
        ret_val = self.reset_color()
        ret_val += self.foreground_color(console_color.foreground, check_last)
        ret_val += self.background_color(console_color.background, check_last)
        return ret_val

    def print(self, *args, sep=" ", end="", color: Union[ConsoleColor, None] = None):
        # print(f"sep: {sep} end: {end}, color: {color} args: {args}")
        if color is None or color.no_color():
            print(*args, sep=sep, end=end, file=self.file, flush=True)
        else:
            color = self.color(color)
            print(color)
            print(
                color + " ".join(map(str, args)) + self.RESET,
                sep=sep,
                end=end,
                file=self.file,
                flush=True,
            )

    def set_foreground(self, color):
        fg_color = self.foreground_color(color)
        if fg_color != "":
            print(fg_color, end="", file=self.file)

    def set_background(self, color):
        bg_color = self.background_color(color)
        if bg_color != "":
            print(bg_color, end="", file=self.file)

    def reset_color(self):
        self.console_color.reset()
        return self.RESET

    def move_up(self, cells: int = 1):
        return f"\x1B[{cells}A"

    def move_down(self, cells: int = 1):
        return f"\x1B[{cells}B"

    def move_right(self, cells: int = 1) -> str:
        if cells != 0:
            return f"\x1B[{cells}C"
        return ""

    def move_left(self, cells: int = 1) -> str:
        if cells != 0:
            return f"\x1B[{cells}D"
        return ""

    def move_line_down(self, lines: int = 1):
        return f"\x1B[{lines}E"  # not ANSI.SYS

    def move_line_up(self, lines: int = 1):
        return f"\x1B[{lines}F"  # not ANSI.SYS

    def move_column_absolute(self, column: int = 1):
        return f"\x1B[{column}G"  # not ANSI.SYS

    def move_cursor(self, row: int = 0, column: int = 0):
        # 0-based to 1-based
        print(f"\x1B[{row + 1};{column + 1}H", end="", file=self.file)

    def horizontal_vertical_position(self, row: int = 0, column: int = 0):
        # 0-based to 1-based
        print(f"\x1B[{row + 1};{column + 1}f", end="", file=self.file)

    @staticmethod
    def cursor_hide():
        print("\x1b[?25l")
        # alternative on windows without vt:
        # https://docs.microsoft.com/en-us/windows/console/setconsolecursorinfo?redirectedfrom=MSDN

    @staticmethod
    def cursor_show():
        print("\x1b[?25h")


class Test:
    @staticmethod
    def color_line(start, end, text="  ", use_color=False, width=2):
        for color in range(start, end):
            print(
                f'\x1B[48;5;{color}m{("{:" + str(width) + "}").format(color) if use_color else text}',
                end="",
            )
        print("\x1B[0m")

    @staticmethod
    def color_line_24bit(start, end, step=0):
        for color in range(start, end, step):
            print(f"\x1B[48;2;{color};{color};{color}mXD", end="")
        print("\x1B[0m")


# TODO:
# CMD - mouse coordinates include a big buffer scroll up, so instead of 30 we get 1300 for y-val
# Windows Terminal - correct coord

# BUG Windows Terminal
# CMD - generates EventType 0x10 on focus or loss with ENABLE_QUICK_EDIT_MODE
# Terminal - nothing
# without quick edit mode the event for focus loss is not raised
# however this is internal event and should be ignored according to msdn
