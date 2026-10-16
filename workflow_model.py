"""Workflow editor fields and defaults, independent of the UI toolkit."""
import copy

FIELDS = [
    ("id", "Step ID"), ("type", "Action"), ("image", "Image file"),
    ("threshold", "Similarity (0..1)"), ("retries", "Attempts"),
    ("retry_delay", "Retry delay (s)"), ("click_match", "Tap matched image"),
    ("offset_x", "Tap offset X"), ("offset_y", "Tap offset Y"),
    ("region", "Region x,y,w,h"),
    ("on_true", "If found: step ID"), ("on_false", "If missing: step ID"),
    ("next", "Next step ID"), ("seconds", "Wait seconds"),
    ("x", "X"), ("y", "Y"), ("x2", "End X"), ("y2", "End Y"),
    ("duration", "Swipe milliseconds"), ("key", "Android key code"),
    ("text", "Input text"),
]
FIELDS_BY_TYPE = {
    "find_image": ["image", "threshold", "retries", "retry_delay", "click_match", "offset_x", "offset_y", "region", "on_true", "on_false", "next"],
    "tap": ["x", "y", "next"], "wait": ["seconds", "next"],
    "swipe": ["x", "y", "x2", "y2", "duration", "next"],
    "key": ["key", "next"], "text": ["text", "next"],
    "goto": ["next"], "stop": [],
}
DEFAULTS = {
    "find_image": {"image": "", "threshold": 0.88, "retries": 5, "retry_delay": 0.8, "click_match": True, "offset_x": 0, "offset_y": 0, "region": "", "on_true": "", "on_false": "", "next": ""},
    "tap": {"x": 200, "y": 200, "next": ""},
    "wait": {"seconds": 1.0, "next": ""},
    "swipe": {"x": 250, "y": 900, "x2": 250, "y2": 300, "duration": 450, "next": ""},
    "key": {"key": "KEYCODE_BACK", "next": ""},
    "text": {"text": "hello", "next": ""},
    "goto": {"next": ""}, "stop": {},
}


def new_step(kind, taken):
    i = 1
    while f"step_{i}" in taken:
        i += 1
    return {"id": f"step_{i}", "type": kind, **copy.deepcopy(DEFAULTS[kind])}


