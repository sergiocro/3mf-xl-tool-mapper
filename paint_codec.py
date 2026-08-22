from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable


class PaintCodecError(ValueError):
    pass


@dataclass(frozen=True)
class PaintNode:
    split_sides: int = 0
    special_side: int = 0
    state: int = 0
    children: tuple["PaintNode", ...] = ()


@dataclass(frozen=True)
class PaintCode:
    root: PaintNode
    lowercase: bool = False


def decode_paint_color(value: str) -> PaintCode:
    if not value or any(ch not in "0123456789abcdefABCDEF" for ch in value):
        raise PaintCodecError("paint_color mora biti neprazan heksadecimalni string.")
    nibbles = [int(ch, 16) for ch in reversed(value)]
    position = 0

    def decode_node() -> PaintNode:
        nonlocal position
        if position >= len(nibbles):
            raise PaintCodecError("Neočekivan kraj paint_color bitstreama.")
        code = nibbles[position]
        position += 1
        split_sides = code & 0b11
        if split_sides:
            special_side = code >> 2
            children = tuple(decode_node() for _ in range(split_sides + 1))
            return PaintNode(split_sides=split_sides, special_side=special_side, children=children)
        upper = code >> 2
        if upper != 0b11:
            return PaintNode(state=upper)
        state = 3
        while True:
            if position >= len(nibbles):
                raise PaintCodecError("Nedostaje extended-state nibble.")
            extension = nibbles[position]
            position += 1
            if extension == 0xF:
                state += 15
            else:
                state += extension
                break
        return PaintNode(state=state)

    try:
        root = decode_node()
    except RecursionError as exc:
        raise PaintCodecError("paint_color subdivision stablo je preduboko.") from exc
    if position != len(nibbles):
        raise PaintCodecError(f"paint_color sadrži {len(nibbles) - position} neiskorištenih nibbleova.")
    lowercase = any(ch in "abcdef" for ch in value) and not any(ch in "ABCDEF" for ch in value)
    return PaintCode(root, lowercase)


def encode_paint_color(code: PaintCode) -> str:
    nibbles: list[int] = []

    def encode_node(node: PaintNode) -> None:
        if node.split_sides:
            if node.split_sides not in (1, 2, 3) or not 0 <= node.special_side <= 3:
                raise PaintCodecError("Nevalidan paint_color split node.")
            if len(node.children) != node.split_sides + 1:
                raise PaintCodecError("Pogrešan broj djece paint_color split nodea.")
            nibbles.append(node.split_sides | (node.special_side << 2))
            for child in node.children:
                encode_node(child)
            return
        if node.children or node.state < 0:
            raise PaintCodecError("Nevalidan paint_color leaf node.")
        if node.state < 3:
            nibbles.append(node.state << 2)
            return
        nibbles.append(0xC)
        extension = node.state - 3
        while extension >= 15:
            nibbles.append(0xF)
            extension -= 15
        nibbles.append(extension)

    try:
        encode_node(code.root)
    except RecursionError as exc:
        raise PaintCodecError("paint_color subdivision stablo je preduboko.") from exc
    digits = "0123456789abcdef" if code.lowercase else "0123456789ABCDEF"
    return "".join(digits[nibble] for nibble in reversed(nibbles))


def remap_paint_color(code: PaintCode, mapping: dict[int, int]) -> PaintCode:
    def remap(node: PaintNode) -> PaintNode:
        if node.split_sides:
            return replace(node, children=tuple(remap(child) for child in node.children))
        if node.state == 0 or node.state not in mapping:
            return node
        return replace(node, state=mapping[node.state])

    try:
        return replace(code, root=remap(code.root))
    except RecursionError as exc:
        raise PaintCodecError("paint_color subdivision stablo je preduboko.") from exc


def paint_states(code: PaintCode) -> Iterable[int]:
    stack = [code.root]
    while stack:
        node = stack.pop()
        if node.split_sides:
            stack.extend(reversed(node.children))
        else:
            yield node.state
