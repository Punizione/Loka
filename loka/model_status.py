"""Shared model status messages."""

UNSUPPORTED_MODEL_MESSAGE = '此版本不支持此类MOD。请自行联系原MOD作者'


class UnsupportedModelError(ValueError):
    def __init__(self):
        super().__init__(UNSUPPORTED_MODEL_MESSAGE)
