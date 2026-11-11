"""Translate only system spans marked while a handoff is assembled.

Source strings never enter a translation lookup. Stored offsets allow a language
change to re-render the same immutable card without creating or confirming it.
"""
from fastapi import HTTPException


def check_language(language):
    if language not in ('zh', 'en'):
        raise HTTPException(422, 'language must be zh or en')
    return language


class CardText(str):
    def __new__(cls, text='', spans=()):
        value = super().__new__(cls, text)
        value.spans = list(spans)
        return value

    def __add__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        offset = len(self)
        return CardText(str(self) + str(other), self.spans + [
            [start + offset, end + offset, expected, translated]
            for start, end, expected, translated in getattr(other, 'spans', ())
        ])

    def __radd__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return CardText(other) + self

    def join(self, pieces):
        chunks, spans, offset = [], [], 0
        for index, piece in enumerate(pieces):
            for fragment in ([self, piece] if index else [piece]):
                chunks.append(str(fragment))
                spans.extend([start + offset, end + offset, expected, translated]
                             for start, end, expected, translated in getattr(fragment, 'spans', ()))
                offset += len(fragment)
        return CardText(''.join(chunks), spans)


def system(zh, en):
    return CardText(zh, [[0, len(zh), zh, en]])


def render(text, spans, language):
    check_language(language)
    if language == 'zh' or not isinstance(spans, list):
        return str(text)
    chunks, position = [], 0
    for span in spans:
        if not isinstance(span, list) or len(span) != 4:
            return str(text)
        start, end, expected, translated = span
        if (type(start) is not int or type(end) is not int or not position <= start < end <= len(text)
                or not isinstance(expected, str) or not isinstance(translated, str) or text[start:end] != expected):
            # Unknown/edited legacy data stays verbatim; never guess ownership.
            return str(text)
        chunks.extend([text[position:start], translated])
        position = end
    chunks.append(text[position:])
    return ''.join(chunks)
