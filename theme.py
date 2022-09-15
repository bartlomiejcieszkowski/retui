from abc import ABC
from enum import IntEnum

from ascii_painter_engine import ConsoleColor, Color


class Selector:
    def __init__(self, element_name: str, element_id: str, element_classes: list[str]):
        self.element_name = element_name
        self.element_id = element_id
        self.element_classes = element_classes


class Attributes:
    def __init__(self):
        self.color = ConsoleColor(fgcolor=Color(None), bgcolor=Color(None))

    def __add__(self, other):
        if other.color.fgcolor is not None:
            self.color.fgcolor = other.color.fgcolor
        if other.color.bgcolor is not None:
            self.color.bgcolor = other.color.bgcolor


class Selectors(ABC):
    def __init__(self):
        # selectors are inspired by css
        # TODO: allow styling by css stylesheet, but with limited subset
        self.selectors = {}
        self.id_selectors = {}
        self.class_selectors = {}
        self.universal_selector = None

    def add_selector(self, name: str, attributes):
        if name == '*':
            self.universal_selector = attributes
        elif name.startswith('#'):
            self.id_selectors[name] = attributes
        elif name.startswith('.'):
            self.class_selectors[name] = attributes
        else:
            self.selectors[name] = attributes

    def effective_selector(self, selector: Selector):
        none_attributes = Attributes()
        attributes = Attributes()
        attributes += self.universal_selector
        attributes += self.selectors.get(selector.element_name, none_attributes)
        for name in selector.element_classes:
            attributes += self.class_selectors.get(name, none_attributes)
        attributes += self.id_selectors.get(name, none_attributes)
        return attributes


class State(IntEnum):
    selector = 0,
    open_sect = 1,
    property = 2,
    value = 3,
    colon = 4
    semi_colon = 5,
    comment = 6


class CssParser:
    @staticmethod
    def parse(file_name: str, selectors: Selectors) -> Selectors:
        if selectors is None:
            selectors = Selectors()
        with open(file_name, 'r') as f:
            last_state = State.selector
            state = State.selector
            selector = None
            prop = None
            value = None
            failed = None
            line_num = 0
            word = ''
            non_printables = ['\n', '\r', '\t']
            for line in f:
                line_num += 1
                idx = 0
                end = len(line)
                while idx < end:
                    c = line[idx]
                    # big switch goes here
                    if state == State.comment:
                        if c == '*':
                            # hope
                            if idx + 1 < end:
                                c_next = line[idx+1]
                                if c_next == '/':
                                    # comment end
                                    # restore state and skip */
                                    state = last_state
                                    idx += 2
                                    continue
                        idx += 1
                        continue

                    if c == '/':
                        # comment?
                        if idx+1 < end:
                            # comment can't span to next line
                            c_next = line[idx+1]
                            if c_next == '*':
                                # comment start
                                # store state and skip /*
                                last_state = state
                                state = State.comment
                                idx += 2
                                continue

                    if c in non_printables:
                        c = ' '

                    if state == State.selector:
                        word_len = len(word)
                        if len(word) > 0:
                            if c == '{':
                                # '*{'
                                #   ^
                                state = State.open_sect
                                # ommit increment - we will hit the switch for {
                                continue
                            elif c != ' ':
                                word += c
                            else:
                                state = State.open_sect
                        elif c == ' ':
                            # '    * {'
                            #  ^^^^
                            pass
                        else:
                            # '    * {'
                            #      ^
                            word += c
                        idx += 1
                        continue
                    elif state == State.open_sect:
                        if c == ' ':
                            # skipping spaces
                            pass
                        elif c == '{':
                            selector = word
                            word = ''
                            state = State.property
                            pass
                        else:
                            failed = Exception(f'{line_num}: state: {state} - got "{c}" - line: "{line}"')
                            break
                        idx += 1
                        continue
                    elif state == State.property:
                        if c == ':':
                            state = State.colon
                            continue
                        elif c == '}':
                            state = State.selector
                        elif c == ' ':
                            # yes, i know that this will remove spaces
                            pass
                        else:
                            word += c
                        idx += 1
                        continue
                    elif state == State.colon:
                        if c == ':':
                            prop = word
                            word = ''
                            state = State.value
                        elif c == ' ':
                            pass
                        else:
                            failed = Exception(f'{line_num}: state: {state} - got "{c}" - line: "{line}"')
                            break
                        idx += 1
                        continue
                    elif state == State.value:
                        if c == ' ':
                            pass
                        elif c == ';':
                            state = State.semi_colon
                            continue
                        else:
                            word += c
                        idx += 1
                        continue
                    elif state == State.semi_colon:
                        if c == ';':
                            value = word
                            word = ''
                            state = State.property
                            print(f'{selector} {{ {prop}: {value}; }}')
                            # TODO: PARSE PROPERTY
                        elif c == ' ':
                            pass
                        else:
                            failed = Exception(f'{line_num}: state: {state} - got "{c}" - line: "{line}"')
                            break
                        idx += 1
                        continue
                    else:
                        failed = Exception(f'UNHANDLED STATE: {state}')
                        break
                if failed:
                    break

            if failed:
                # cleanup goes here
                raise failed

        return selectors