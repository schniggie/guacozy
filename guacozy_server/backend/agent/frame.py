import base64
from io import BytesIO

from PIL import Image


class Frame:
    def __init__(self):
        self._layers: dict[int, Image.Image] = {}
        self._rects: dict[int, tuple[int, int, int, int]] = {}
        self._streams: dict[str, dict] = {}
        self._cursor = (0, 0)

    @property
    def cursor(self) -> tuple[int, int]:
        return self._cursor

    @property
    def size(self) -> tuple[int, int]:
        layer = self._layers.get(0)
        if layer is None:
            return (0, 0)
        return layer.size

    def apply(self, instruction) -> None:
        opcode = instruction.opcode
        args = instruction.args
        if opcode == "size":
            self._size(*args)
        elif opcode == "img":
            self._img(*args)
        elif opcode == "blob":
            self._blob(*args)
        elif opcode == "end":
            self._end(*args)
        elif opcode == "rect":
            self._rect(*args)
        elif opcode == "cfill":
            self._cfill(*args)
        elif opcode == "copy":
            self._copy(*args)
        elif opcode == "cursor":
            self._cursor_op(*args)

    def png_bytes(self) -> bytes:
        layer = self._layers.get(0)
        if layer is None or layer.width == 0 or layer.height == 0:
            raise ValueError("layer 0 has no size")
        buffer = BytesIO()
        layer.save(buffer, format="PNG")
        return buffer.getvalue()

    def _layer(self, index: int) -> Image.Image:
        layer = self._layers.get(index)
        if layer is None:
            layer = Image.new("RGBA", (0, 0), (0, 0, 0, 0))
            self._layers[index] = layer
        return layer

    def _size(self, layer, width, height):
        index = int(layer)
        width = int(width)
        height = int(height)
        existing = self._layers.get(index)
        if existing is not None and existing.size == (width, height):
            return
        resized = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        # Keep the top-left pixels that still fit. New pixels stay transparent.
        if (
            existing is not None
            and existing.width
            and existing.height
            and width
            and height
        ):
            resized.paste(existing, (0, 0))
        self._layers[index] = resized

    def _img(self, stream, mode, layer, mime, x, y):
        if mime != "image/png":
            raise ValueError(f"unsupported mime: {mime}")
        # Composite mode is accepted and ignored. The PNG is pasted with its own alpha.
        int(mode)
        self._streams[str(stream)] = {
            "layer": int(layer),
            "x": int(x),
            "y": int(y),
            "parts": [],
        }

    def _blob(self, stream, data):
        entry = self._streams.get(str(stream))
        if entry is None:
            return
        entry["parts"].append(base64.b64decode(data))

    def _end(self, stream):
        entry = self._streams.pop(str(stream), None)
        if entry is None:
            return
        payload = b"".join(entry["parts"])
        with Image.open(BytesIO(payload)) as image:
            patch = image.convert("RGBA")
        self._paint(entry["layer"], patch, entry["x"], entry["y"], replace=False)

    def _rect(self, layer, x, y, width, height):
        self._rects[int(layer)] = (int(x), int(y), int(width), int(height))

    def _cfill(self, mask, layer, red, green, blue, alpha):
        int(mask)
        rect = self._rects.get(int(layer))
        if rect is None:
            return
        x, y, width, height = rect
        if width <= 0 or height <= 0:
            return
        color = (int(red), int(green), int(blue), int(alpha))
        overlay = Image.new("RGBA", (width, height), color)
        self._paint(int(layer), overlay, x, y, replace=True)

    def _copy(
        self, src_layer, src_x, src_y, width, height, mask, dst_layer, dst_x, dst_y,
    ):
        int(mask)
        src = self._layer(int(src_layer))
        sx, sy = int(src_x), int(src_y)
        sw, sh = int(width), int(height)
        dx, dy = int(dst_x), int(dst_y)
        if sw <= 0 or sh <= 0:
            return
        if sx < 0:
            sw += sx
            dx -= sx
            sx = 0
        if sy < 0:
            sh += sy
            dy -= sy
            sy = 0
        if sw <= 0 or sh <= 0 or sx >= src.width or sy >= src.height:
            return
        sw = min(sw, src.width - sx)
        sh = min(sh, src.height - sy)
        if sw <= 0 or sh <= 0:
            return
        patch = src.crop((sx, sy, sx + sw, sy + sh))
        self._paint(int(dst_layer), patch, dx, dy, replace=True)

    def _cursor_op(self, x, y, *_source):
        # x, y are the hotspot. The source rectangle is the pointer image and is not drawn.
        self._cursor = (int(x), int(y))

    def _paint(self, layer_index, patch, x, y, *, replace):
        dest = self._layer(layer_index)
        if patch.mode != "RGBA":
            patch = patch.convert("RGBA")
        src_x = 0
        src_y = 0
        if x < 0:
            src_x = -x
            x = 0
        if y < 0:
            src_y = -y
            y = 0
        if dest.width == 0 or dest.height == 0 or x >= dest.width or y >= dest.height:
            return
        width = min(patch.width - src_x, dest.width - x)
        height = min(patch.height - src_y, dest.height - y)
        if width <= 0 or height <= 0:
            return
        if src_x or src_y or width != patch.width or height != patch.height:
            patch = patch.crop((src_x, src_y, src_x + width, src_y + height))
        if replace:
            dest.paste(patch, (x, y))
        else:
            dest.paste(patch, (x, y), patch)
