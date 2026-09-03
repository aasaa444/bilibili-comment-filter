from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

WBI_KEY_PERMUTATION: tuple[int, ...] = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)

_WBI_UNSAFE_CHARS = re.compile(r"[!'()*]")


@dataclass(frozen=True)
class BilibiliWbiSigner:
    """Build the WBI query signature used by Bilibili's web comment API."""

    image_key: str
    sub_key: str

    @classmethod
    def from_urls(cls, image_url: str, sub_url: str) -> BilibiliWbiSigner:
        return cls(_key_from_url(image_url), _key_from_url(sub_url))

    @property
    def mixin_key(self) -> str:
        combined = self.image_key + self.sub_key
        if len(combined) <= max(WBI_KEY_PERMUTATION):
            raise ValueError("Bilibili WBI keys are too short")
        return "".join(combined[index] for index in WBI_KEY_PERMUTATION)[:32]

    def sign(
        self,
        params: Mapping[str, object],
        *,
        timestamp: int | None = None,
    ) -> dict[str, str]:
        """Return a new signed parameter mapping without mutating the caller's data."""

        signed = {
            str(key): _WBI_UNSAFE_CHARS.sub("", str(value))
            for key, value in params.items()
            if key != "w_rid"
        }
        signed["wts"] = str(int(time.time() if timestamp is None else timestamp))
        canonical = dict(sorted(signed.items()))
        query = urlencode(canonical, quote_via=quote)
        canonical["w_rid"] = hashlib.md5(
            (query + self.mixin_key).encode("utf-8")
        ).hexdigest()
        return canonical


def _key_from_url(url: str) -> str:
    filename = urlsplit(url).path.rsplit("/", 1)[-1]
    key = filename.rsplit(".", 1)[0]
    if not key:
        raise ValueError("Bilibili WBI key URL did not contain a filename")
    return key
