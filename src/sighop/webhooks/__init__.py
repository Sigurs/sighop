"""Webhooks: telling systems outside sighop what happened on the mesh.

A contact-store listener turns a first verified sighting into an event
(`triggers`, `events`), a pure renderer turns the event into a body
(`render`), and one delivery task POSTs it off the reception path with bounded
retries (`dispatcher`, `transport`). Configuration is stored in the `webhook`
table and validated by `config`, shared by the command line and the panel.
"""
