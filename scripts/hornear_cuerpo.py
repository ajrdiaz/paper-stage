"""Genera assets/modelos/cuerpo-torso.js desde «Universal Base Characters» de Quaternius (CC0; versión gratuita en itch.io):
pose relajada con los brazos abajo, sin ojos ni cejas y de cintura para arriba.

    .venv/bin/python scripts/hornear_cuerpo.py ".../Superhero_Male_FullBody.gltf" assets/modelos/cuerpo-torso.js
"""
import base64
import json
import struct
import sys
from pathlib import Path

import numpy as np

path, OUT = Path(sys.argv[1]), Path(sys.argv[2])
g = json.loads(path.read_text())
bin_ = (path.parent / g["buffers"][0]["uri"]).read_bytes()
SIZES = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
TYPES = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}


def acc(i):
    a = g["accessors"][i]
    v = g["bufferViews"][a["bufferView"]]
    n = SIZES[a["type"]]
    t = TYPES[a["componentType"]]
    data = np.frombuffer(bin_, t, a["count"] * n, v.get("byteOffset", 0) + a.get("byteOffset", 0))
    return data.reshape(-1, n) if n > 1 else data


nodes = g["nodes"]
parents = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}


def local(node, extra=None):
    T = np.eye(4)
    T[:3, 3] = node.get("translation", [0, 0, 0])
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    R = np.eye(4)
    R[:3, :3] = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                 [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                 [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    S = np.diag([*node.get("scale", [1, 1, 1]), 1])
    return T @ R @ S


def world(i, cache, bend):
    if i in cache:
        return cache[i]
    m = local(nodes[i])
    if i in bend:
        m = m @ bend[i]
    if i in parents:
        m = world(parents[i], cache, bend) @ m
    cache[i] = m
    return m


def rot_z(deg):
    a = np.radians(deg)
    m = np.eye(4)
    m[:2, :2] = [[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]]
    return m


skin = g["skins"][0]
joints = skin["joints"]
inverse = acc(skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
names = {nodes[j].get("name", ""): j for j in joints}
print("huesos de brazo:", [n for n in names if "arm" in n.lower() or "shoulder" in n.lower() or "clav" in n.lower()][:10])


def base_pos(prim):
    return acc(prim["attributes"]["POSITION"]).astype(np.float64)


print("mallas:", [(m.get("name"), [g["accessors"][q["attributes"]["POSITION"]]["count"] for q in m["primitives"]]) for m in g["meshes"]])
# La malla más grande es el cuerpo; ojos y cejas quedan fuera.
prim = max((q for m in g["meshes"] for q in m["primitives"]), key=lambda q: g["accessors"][q["attributes"]["POSITION"]]["count"])
pos, jnt, wgt, ind = base_pos(prim), acc(prim["attributes"]["JOINTS_0"]), acc(prim["attributes"]["WEIGHTS_0"]), acc(prim["indices"])
hand_ids = [names[n] for n in names if "upperarm" in n.lower()]


def bake(left_deg, right_deg):
    bend = {}
    for name, j in names.items():
        low = name.lower()
        if "upperarm" in low:
            bend[j] = rot_z(left_deg if low.endswith(("_l", ".l", "l")) else right_deg)
    cache = {}
    mats = np.stack([world(j, cache, bend) @ inverse[k] for k, j in enumerate(joints)])
    homog = np.c_[pos, np.ones(len(pos))]
    return np.einsum("ni,nij,nj->nj", wgt, mats[jnt], homog)[:, :3] if False else sum(
        np.einsum("n,nij,nj->ni", wgt[:, s], mats[jnt[:, s]], homog)[:, :3] for s in range(4))


# El signo del giro depende del rig: se prueba cuál baja de verdad las manos.
best = None
for a, b in ((-72, 72), (72, -72)):
    baked = bake(a, b)
    lowest_hand = baked[np.abs(baked[:, 0]) > .25, 1].max() if (np.abs(baked[:, 0]) > .25).any() else 0
    width = baked[:, 0].max() - baked[:, 0].min()
    print(f"giro {a}/{b}: ancho {width:.2f}")
    if best is None or width < best[0]:
        best = (width, baked)
baked = best[1]
y0 = baked[:, 1].min()
H = baked[:, 1].max() - y0
print(f"alto {H:.2f} | ancho {best[0]:.2f} | triángulos {len(ind) // 3}")

# De la cintura para arriba: es lo que necesitan los videos y evita el contorno de la entrepierna.
# Por debajo de la cintura solo quedan los brazos: el contorno de la entrepierna no aporta y puede dar problemas de moderación.
cut = y0 + H * .55
tri = ind.reshape(-1, 3)
keep = tri[((baked[tri, 1] >= cut) | (np.abs(baked[tri, 0]) >= .25)).all(axis=1)]
used, remap = np.unique(keep, return_inverse=True)
vertices = baked[used].astype(np.float32)
vertices[:, 1] -= cut  # el origen queda en la cintura

# Normales suavizadas, recalculadas tras la pose.
tris = remap.reshape(-1, 3)
normals = np.zeros_like(vertices, dtype=np.float64)
cross = np.cross(vertices[tris[:, 1]] - vertices[tris[:, 0]], vertices[tris[:, 2]] - vertices[tris[:, 0]])
for c in range(3):
    np.add.at(normals, tris[:, c], cross)
normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-9)
normals = normals.astype(np.float32)
print(f"torso: {len(tris)} triángulos, {len(vertices)} vértices, alto {vertices[:, 1].max():.2f}")

js = ("// Torso humano de una sola superficie, listo para Three.js. Derivado de «Universal Base Characters» de Quaternius, CC0 1.0 (dominio público).\n"
      "// Uso: const geo = window.CUERPO_TORSO(THREE); el origen está en la cintura y la cabeza mira a +Z.\n"
      "window.CUERPO_TORSO=function(T){const d=(s,A)=>{const b=Uint8Array.from(atob(s),c=>c.charCodeAt(0));return new A(b.buffer)};const g=new T.BufferGeometry();"
      f"g.setAttribute('position',new T.BufferAttribute(d('{base64.b64encode(vertices.tobytes()).decode()}',Float32Array),3));"
      f"g.setAttribute('normal',new T.BufferAttribute(d('{base64.b64encode(normals.tobytes()).decode()}',Float32Array),3));"
      f"g.setIndex(new T.BufferAttribute(d('{base64.b64encode(tris.astype(np.uint32).tobytes()).decode()}',Uint32Array),1));g.computeBoundingBox();return g}};\n")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(js)
print("módulo:", round(len(js) / 1e6, 2), "MB →", OUT)
