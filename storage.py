import json
import os
import threading
from config import DATA_FILE

_file_lock = threading.Lock()


def _load(path):
    with _file_lock:
        if not os.path.exists(path):
            return {}
        with open(path, "r") as f:
            return json.load(f)


def _save(path, data):
    with _file_lock:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


def load():
    return _load(DATA_FILE)


def save(data):
    return _save(DATA_FILE, data)


def user_gang(uid, data):
    uid = str(uid)
    for gid, gang in data.items():
        if gang["leader"] == uid:
            return gid, gang
    return None, None


def member_gang(uid, data):
    uid = str(uid)
    for gid, gang in data.items():
        if uid in gang["members"]:
            return gid, gang
    return None, None


def name_exists(name, data, exclude_gid=None):
    name = name.strip().lower()
    for gid, gang in data.items():
        if exclude_gid is not None and gid == exclude_gid:
            continue
        if gang.get("name", "").strip().lower() == name:
            return True
    return False
