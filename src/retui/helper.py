import json

from . import (  # noqa: F401
    App,
    Color,
    ColorBits,
    ConsoleColor,
    ConsoleWidget,
    mapping,
    no_print,
    widgets,
)
from .mapping import get_mapping, is_mapping, register_mapping_dict

FUNCTION_THIS_ARG = "##this"
KEY_POST_CALLBACKS = "post_callbacks"


def register_app_dict(name, app_dict):
    register_mapping_dict(name, app_dict)


def __callback_wrapper(function, *args):
    print(f"{function} - type({type(function)})")
    print(f"{args} - type({type(args)})")
    function(*args)


def __post_callback(this_json, this):
    post_callbacks = this_json.get(KEY_POST_CALLBACKS)
    if post_callbacks:
        for callback in post_callbacks:
            print(callback)
            for key, value in callback.items():
                if is_mapping(value):
                    callback[key] = get_mapping(value)

            fun = callback.get("function", None)
            args = None
            if callable(fun):
                callback_args = callback.get("args", None)
                if callback_args:
                    if isinstance(callback_args, list):
                        args = []
                        # replace __this__
                        for arg in callback_args:
                            if is_mapping(arg):
                                arg = get_mapping(arg)
                            if isinstance(arg, str):
                                if arg == FUNCTION_THIS_ARG:
                                    args.append(this)
                                    continue
                            args.append(arg)
                    elif isinstance(callback_args, dict):
                        raise Exception("Dict is not implemented.")
                    else:
                        raise Exception("Unsupported args type.")
            __callback_wrapper(fun, *args)


def app_from_json(
    filename, ctx_globals=None, log_fun=no_print, app_dict_name="main", app_dict=None, debug: bool = False
):
    if app_dict_name and app_dict:
        register_app_dict(app_dict_name, app_dict)

    with open(filename, "r") as f:
        app_json = json.load(f)

        # TODO: validate
        app = App(log=log_fun, debug=debug)
        title = app_json["name"]
        if "title" in app_json:
            if len(app_json["title"]) > 0:
                title = app_json["title"]

        app.title = title
        app.color_mode(app_json.get("color", True))

        widget_id_dict = {}

        # widgets
        for widget_json in app_json["widgets"]:
            if widget_json.get("ignore", False):
                continue
            # mapping app dict values
            for key, value in widget_json.items():
                if is_mapping(value):
                    widget_json[key] = get_mapping(value)

            widget_type = widget_json.get("type", None)
            if isinstance(widget_type, str):
                widget_class = mapping.get_widget_class(widget_type)
                if widget_class is None:
                    widget_class = mapping.import_widget_class(widget_type, ctx_globals)
                    if widget_class is None:
                        raise Exception(f"Unknown widget type: '{widget_type}'")
            elif issubclass(type(widget_type), ConsoleWidget):
                widget_class = widget_type
            else:
                raise Exception(f"widget[{widget_json['id']}] is of type: {type(widget_type)}")

            widget_json["app"] = app

            if "border_color" in widget_json:
                fg_color = Color(
                    widget_json["border_color"]["fg"]["val"], ColorBits[widget_json["border_color"]["fg"]["color_bits"]]
                )
                bg_color = Color(
                    widget_json["border_color"]["bg"]["val"], ColorBits[widget_json["border_color"]["bg"]["color_bits"]]
                )
                widget_json["border_color"] = ConsoleColor(fg_color, bg_color)

            widget = widget_class.from_dict(**widget_json)

            widget_id = widget_json.get("id", None)
            if widget_id:
                widget_id_dict[widget_id] = widget
            # if has id - add it to dict
            parent = app
            parent_id = widget_json.get("parent_id", None)
            if parent_id:
                parent = widget_id_dict.get(parent_id, None)
                if parent is None:
                    raise Exception(f"Given parent_id: '{parent_id}' doesnt match already defined id of widget")
            parent.add_widget(widget)
            __post_callback(widget_json, widget)

        __post_callback(app_json, app)
        return app
