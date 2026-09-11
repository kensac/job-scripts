"""Turning a raw message into something the pipeline can read.

`importer` carries a mailbox export into the store, `html` derives the text a
classifier reads from the markup that must be kept, and `prefilter` is the
cheap pass that decides a message is not worth a model at all.
"""
