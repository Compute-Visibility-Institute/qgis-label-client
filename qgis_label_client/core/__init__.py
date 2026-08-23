"""Pure-Python core of the CVI label client.

Nothing in this subpackage imports ``qgis`` or ``qgis.PyQt``. That is a deliberate,
enforced boundary and not an accident of layering:

* it is the half of the plugin that can be unit-tested without a running QGIS, which
  is the only kind of test CI can run;
* URI construction, URL matching and coverage classification are exactly the places
  where a silent mistake is expensive (a malformed OAPIF URI fails loudly, but a
  wrongly-matched signed URL or a mis-classified survey extent does not).

Everything that needs Qt or the QGIS API lives one level up.
"""

from __future__ import annotations
