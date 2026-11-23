"""FR5 joint frames, inertial assumptions and XML helpers."""

import xml.etree.ElementTree as ET

# Joint origins, axes, and limits transcribed from fairino5_v6.urdf at the revision in assets.py.
# Official FR5 STL files are visuals; link collision uses conservative primitives.
JOINTS = [
    ("j1", (0, 0, 0), (0, 0, 0), (-3.0543, 3.0543)),
    ("j2", (0, 0, 0.152), (1.5708, 0, 0), (-4.6251, 1.4835)),
    ("j3", (-0.425, 0, 0), (0, 0, 0), (-2.8274, 2.8274)),
    ("j4", (-0.39501, 0, 0), (0, 0, 0), (-4.6251, 1.4835)),
    ("j5", (0, 0, 0.1021), (1.5708, 0, 0), (-3.0543, 3.0543)),
    ("j6", (0, 0, 0.102), (-1.5708, 0, 0), (-3.0543, 3.0543)),
]
LINK_MASS = [1.6185, 4.64, 10.08, 2.71, 1.56, 1.56, 0.36]
COLLISION = [
    ("cylinder", "0.07 0.045", "0 0 .045"),
    ("sphere", "0.075", "0 0 0"),
    ("capsule", "0.045", "-.21 0 0"),
    ("capsule", "0.038", "-.19 0 0"),
    ("sphere", "0.058", "0 0 0"),
    ("sphere", "0.052", "0 0 0"),
    ("sphere", "0.04", "0 0 0"),
]


def s(vals):
    return " ".join(str(x) for x in vals)


def sub(parent, tag, **attrs):
    return ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
