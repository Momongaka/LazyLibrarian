#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software, you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

"""
Post-processing utility functions.

This module provides type-safe string/bytes conversion utilities used across
the postprocessing subsystem to handle cross-platform encoding issues.
"""

from typing import Any

from lazylibrarian.formatter import make_unicode, make_utf8bytes


def enforce_str(text: str | bytes | Any) -> str:
    """
    Wrap make_unicode() and enforce that result is a string.

    make_unicode() returns str|bytes|None. This wrapper:
    - Returns string if make_unicode succeeded
    - Decodes bytes if make_unicode returned bytes (decode failure)
    - Raises ValueError if text is None
    - Raises TypeError for other types

    Args:
        text: Input to convert to string

    Returns:
        String value

    Raises:
        ValueError: If text is None
        TypeError: If text cannot be converted to string
    """
    result = make_unicode(text)

    if isinstance(result, str):
        return result

    if isinstance(result, bytes):
        # make_unicode failed to decode, try ourselves with fallback
        try:
            return result.decode("utf-8")
        except UnicodeDecodeError:
            # latin-1 accepts all byte values (0x00-0xFF)
            return result.decode("latin-1")

    if result is None:
        raise ValueError("Cannot convert None to string")

    raise TypeError(f"Expected str, got {type(result).__name__}")


def enforce_bytes(text: str | bytes | Any) -> bytes:
    """
    Wrap make_utf8bytes() and enforce that result is bytes.

    make_utf8bytes() returns tuple[bytes, str] but the bytes can be None.
    This wrapper:
    - Returns bytes if make_utf8bytes succeeded
    - Raises ValueError if input is None
    - Raises TypeError for other types

    Args:
        text: Input to convert to bytes

    Returns:
        Bytes value (UTF-8 encoded)

    Raises:
        ValueError: If text is None
        TypeError: If text cannot be converted to bytes
    """
    if text is None:
        raise ValueError("Cannot convert None to bytes")

    result, _ = make_utf8bytes(text)

    if isinstance(result, bytes):
        return result

    raise TypeError(f"Expected bytes, got {type(result).__name__}")
