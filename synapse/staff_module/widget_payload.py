#
# STAFF mod — widget state-event content builder.  Shared between
# auto-injection (F16) and propagation-on-update (F17).
#

from typing import Any, Dict


def build_widget_state_content(widget: Dict[str, Any], sender: str) -> Dict[str, Any]:
    """Build the `content` payload of a `im.vector.modular.widgets` state
    event for a staff widget definition.
    """
    inner = widget.get("content") or {}
    # If the caller put extra fields (data, type, waitForIframeLoad) into
    # widget.content, we honour them; otherwise we ship a minimal shape
    # that all three Element clients accept.
    payload: Dict[str, Any] = {
        "type": inner.get("type", "m.custom"),
        "url": widget["url"],
        "name": widget["name"],
        "data": inner.get("data", {}),
        "creatorUserId": sender,
    }
    if "waitForIframeLoad" in inner:
        payload["waitForIframeLoad"] = inner["waitForIframeLoad"]
    return payload
