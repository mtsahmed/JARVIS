"""The HUD: a local web page that shows what Jarvis is doing (docs/ui-protocol.md, docs/ui-design.md).

The core never imports this package; it publishes on the event bus (jarvis.core.events) and the HUD
subscribes. Only this package imports the websockets library.
"""
