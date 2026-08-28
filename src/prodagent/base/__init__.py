"""base — the bottom layer: shared vocabulary and mechanical helpers.

Everything here may be imported by any package; base itself imports nothing
from prodagent. Rule of thumb for additions: shared *words* (types, errors,
codecs) belong here; anything with a policy or an opinion belongs one layer up.
"""
