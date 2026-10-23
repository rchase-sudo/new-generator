"""Blender (bpy 5.x) scene for the approved concept layout -- the same build as the Pocahontas Trail model.
Usage: python blender_scene.py <work_dir> <width> <height> <samples> <out.png>
Reads <work_dir>/scene.json, ground.jpg, lawn.png and the site masks written by render3d.prepare()."""
import bpy, bmesh, json, math, os, sys, random
from mathutils import Vector

WORK, RW, RH, SPP, OUT = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
VIEW = 'front'
P = lambda n: os.path.join(WORK, n)
D = json.load(open(P('scene.json')))
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
rnd = random.Random(11)

# ---------------------------------------------------------------- helpers
def new_obj(name, mesh, coll=None):
    o = bpy.data.objects.new(name, mesh); (coll or sc.collection).objects.link(o); return o

def mat(name):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    return m, nt, out

def principled(nt, **kw):
    p = nt.nodes.new('ShaderNodeBsdfPrincipled')
    for k, v in kw.items(): p.inputs[k].default_value = v
    return p

HAZE = (0.62, 0.64, 0.64)
def haze_group():
    g = bpy.data.node_groups.new('Haze', 'ShaderNodeTree')
    g.interface.new_socket('Shader', in_out='INPUT', socket_type='NodeSocketShader')
    g.interface.new_socket('Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
    n = g.nodes; l = g.links
    gi = n.new('NodeGroupInput'); go = n.new('NodeGroupOutput')
    cam = n.new('ShaderNodeCameraData'); lp = n.new('ShaderNodeLightPath')
    mr = n.new('ShaderNodeMapRange'); mr.inputs['From Min'].default_value = 900 if VIEW == 'plan' else 220; mr.inputs['From Max'].default_value = 3000
    mr.inputs['To Min'].default_value = 0; mr.inputs['To Max'].default_value = 1
    pw = n.new('ShaderNodeMath'); pw.operation = 'POWER'; pw.inputs[1].default_value = 0.7
    sc_ = n.new('ShaderNodeMath'); sc_.operation = 'MULTIPLY'; sc_.inputs[1].default_value = 0.55
    m2 = n.new('ShaderNodeMath'); m2.operation = 'MULTIPLY'
    em = n.new('ShaderNodeEmission'); em.inputs['Color'].default_value = (*HAZE, 1); em.inputs['Strength'].default_value = 1.0
    mix = n.new('ShaderNodeMixShader')
    l.new(cam.outputs['View Distance'], mr.inputs['Value'])
    l.new(mr.outputs['Result'], pw.inputs[0]); l.new(pw.outputs[0], sc_.inputs[0])
    l.new(sc_.outputs[0], m2.inputs[0]); l.new(lp.outputs['Is Camera Ray'], m2.inputs[1])
    l.new(m2.outputs[0], mix.inputs['Fac']); l.new(gi.outputs[0], mix.inputs[1]); l.new(em.outputs[0], mix.inputs[2])
    l.new(mix.outputs[0], go.inputs[0])
    return g
HG = haze_group()
def finish(nt, shader, out):
    h = nt.nodes.new('ShaderNodeGroup'); h.node_tree = HG
    nt.links.new(shader.outputs[0], h.inputs[0]); nt.links.new(h.outputs[0], out.inputs['Surface'])

def img_node(nt, path, noncolor=False):
    t = nt.nodes.new('ShaderNodeTexImage'); t.image = bpy.data.images.load(path)
    if noncolor: t.image.colorspace_settings.name = 'Non-Color'
    t.interpolation = 'Cubic'; t.extension = 'CLIP'
    return t

def quad(name, corners, z=0.0):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(c[0], c[1], z) for c in corners], [], [(0, 1, 2, 3)])
    uv = me.uv_layers.new(name='UVMap')
    for i, (u, v) in enumerate([(0, 0), (1, 0), (1, 1), (0, 1)]): uv.data[i].uv = (u, v)
    return new_obj(name, me)

def grass_shader(nt, base_scale=1.0):
    tc = nt.nodes.new('ShaderNodeTexCoord')
    n1 = nt.nodes.new('ShaderNodeTexNoise'); n1.inputs['Scale'].default_value = 0.05 * base_scale; n1.inputs['Detail'].default_value = 6
    n2 = nt.nodes.new('ShaderNodeTexNoise'); n2.inputs['Scale'].default_value = 1.2; n2.inputs['Detail'].default_value = 8
    nt.links.new(tc.outputs['Object'], n1.inputs['Vector']); nt.links.new(tc.outputs['Object'], n2.inputs['Vector'])
    ramp = nt.nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.elements[0].color = (0.045, 0.085, 0.018, 1); ramp.color_ramp.elements[1].color = (0.13, 0.17, 0.045, 1)
    mx = nt.nodes.new('ShaderNodeMath'); mx.operation = 'MULTIPLY_ADD'; mx.inputs[1].default_value = 0.35
    nt.links.new(n2.outputs['Fac'], mx.inputs[0]); nt.links.new(n1.outputs['Fac'], mx.inputs[2])
    nt.links.new(mx.outputs[0], ramp.inputs['Fac'])
    bump = nt.nodes.new('ShaderNodeBump'); bump.inputs['Strength'].default_value = 0.35
    n3 = nt.nodes.new('ShaderNodeTexNoise'); n3.inputs['Scale'].default_value = 60
    nt.links.new(tc.outputs['Object'], n3.inputs['Vector']); nt.links.new(n3.outputs['Fac'], bump.inputs['Height'])
    p = principled(nt, Roughness=0.92)
    p.inputs['Specular IOR Level'].default_value = 0.25
    nt.links.new(ramp.outputs['Color'], p.inputs['Base Color']); nt.links.new(bump.outputs['Normal'], p.inputs['Normal'])
    return p

# ---------------------------------------------------------------- ground (aerial + lawn), and a wide fill plane to the horizon
C = D['ground']['corners']
g = quad('Ground', C)
m, nt, out = mat('Ground')
tex = img_node(nt, P('ground.jpg'))
lawn = img_node(nt, P('lawn.png'), True)
cc = nt.nodes.new('ShaderNodeHueSaturation'); cc.inputs['Saturation'].default_value = 1.08; cc.inputs['Value'].default_value = 1.05
nt.links.new(tex.outputs['Color'], cc.inputs['Color'])
pa = principled(nt, Roughness=0.9); pa.inputs['Specular IOR Level'].default_value = 0.2
nt.links.new(cc.outputs['Color'], pa.inputs['Base Color'])
pg = grass_shader(nt)
mx = nt.nodes.new('ShaderNodeMixShader')
nt.links.new(lawn.outputs['Color'], mx.inputs['Fac']); nt.links.new(pa.outputs[0], mx.inputs[1]); nt.links.new(pg.outputs[0], mx.inputs[2])
finish(nt, mx, out); g.data.materials.append(m)
xs = [c[0] for c in C]; ys = [c[1] for c in C]
R_ = 6 * max(max(xs) - min(xs), max(ys) - min(ys))
fill = quad('Fill', [(-R_, -R_), (R_, -R_), (R_, R_), (-R_, R_)], -0.08)
m, nt, out = mat('Fill')
nf = nt.nodes.new('ShaderNodeTexNoise'); nf.inputs['Scale'].default_value = 0.02; nf.inputs['Detail'].default_value = 8
tcf = nt.nodes.new('ShaderNodeTexCoord'); nt.links.new(tcf.outputs['Object'], nf.inputs['Vector'])
rf = nt.nodes.new('ShaderNodeValToRGB'); rf.color_ramp.elements[0].color = (0.018, 0.03, 0.012, 1); rf.color_ramp.elements[1].color = (0.05, 0.07, 0.025, 1)
nt.links.new(nf.outputs['Fac'], rf.inputs['Fac'])
pf = principled(nt, Roughness=0.95); nt.links.new(rf.outputs['Color'], pf.inputs['Base Color'])
finish(nt, pf, out); fill.data.materials.append(m)


# ---------------------------------------------------------------- site surface (asphalt / concrete / islands / paint)
(x0, y0), (x1, y1) = D['site_rect']
s = quad('Site', [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], 0.02)
m, nt, out = mat('Site')
A = img_node(nt, P('m_asphalt.png'), True)
Cn = img_node(nt, P('m_concrete.png'), True)
I = img_node(nt, P('m_island.png'), True)
S = img_node(nt, P('m_stripe.png'), True)
for t in (A, Cn, I, S): t.interpolation = 'Linear'
tc = nt.nodes.new('ShaderNodeTexCoord')
# asphalt
na = nt.nodes.new('ShaderNodeTexNoise'); na.inputs['Scale'].default_value = 2.5; na.inputs['Detail'].default_value = 10
nt.links.new(tc.outputs['Object'], na.inputs['Vector'])
ra = nt.nodes.new('ShaderNodeValToRGB'); ra.color_ramp.elements[0].color = (0.028, 0.028, 0.03, 1); ra.color_ramp.elements[1].color = (0.06, 0.06, 0.062, 1)
nt.links.new(na.outputs['Fac'], ra.inputs['Fac'])
pas = principled(nt, Roughness=0.78)
nt.links.new(ra.outputs['Color'], pas.inputs['Base Color'])
ba = nt.nodes.new('ShaderNodeBump'); ba.inputs['Strength'].default_value = 0.15
nn = nt.nodes.new('ShaderNodeTexNoise'); nn.inputs['Scale'].default_value = 140
nt.links.new(tc.outputs['Object'], nn.inputs['Vector']); nt.links.new(nn.outputs['Fac'], ba.inputs['Height']); nt.links.new(ba.outputs['Normal'], pas.inputs['Normal'])
# paint
ppaint = principled(nt, **{'Base Color': (0.82, 0.82, 0.80, 1), 'Roughness': 0.55})
# concrete
nc = nt.nodes.new('ShaderNodeTexNoise'); nc.inputs['Scale'].default_value = 1.5
nt.links.new(tc.outputs['Object'], nc.inputs['Vector'])
rc = nt.nodes.new('ShaderNodeValToRGB'); rc.color_ramp.elements[0].color = (0.42, 0.41, 0.38, 1); rc.color_ramp.elements[1].color = (0.55, 0.54, 0.50, 1)
nt.links.new(nc.outputs['Fac'], rc.inputs['Fac'])
pcon = principled(nt, Roughness=0.8); nt.links.new(rc.outputs['Color'], pcon.inputs['Base Color'])
# island grass (mulch edge feel via darker noise)
pisl = grass_shader(nt, 3.0)
transp = nt.nodes.new('ShaderNodeBsdfTransparent')
m1 = nt.nodes.new('ShaderNodeMixShader'); nt.links.new(I.outputs['Color'], m1.inputs['Fac']); nt.links.new(transp.outputs[0], m1.inputs[1]); nt.links.new(pisl.outputs[0], m1.inputs[2])
m2 = nt.nodes.new('ShaderNodeMixShader'); nt.links.new(Cn.outputs['Color'], m2.inputs['Fac']); nt.links.new(m1.outputs[0], m2.inputs[1]); nt.links.new(pcon.outputs[0], m2.inputs[2])
m3 = nt.nodes.new('ShaderNodeMixShader'); nt.links.new(A.outputs['Color'], m3.inputs['Fac']); nt.links.new(m2.outputs[0], m3.inputs[1]); nt.links.new(pas.outputs[0], m3.inputs[2])
m4 = nt.nodes.new('ShaderNodeMixShader'); nt.links.new(S.outputs['Color'], m4.inputs['Fac']); nt.links.new(m3.outputs[0], m4.inputs[1]); nt.links.new(ppaint.outputs[0], m4.inputs[2])
finish(nt, m4, out); s.data.materials.append(m)

# ---------------------------------------------------------------- curbs
def simple_mat(name, color, rough=0.7, metal=0.0):
    m, nt, out = mat(name)
    p = principled(nt, **{'Base Color': (*color, 1), 'Roughness': rough, 'Metallic': metal})
    finish(nt, p, out); return m
curb_m = simple_mat('Curb', (0.56, 0.55, 0.51), 0.8)
def curb(pts, w=0.15, h=0.15):
    P = [Vector((p[0], p[1], 0)) for p in pts]
    if len(P) < 2: return
    verts, faces = [], []
    for i, p in enumerate(P):
        a = P[max(i - 1, 0)]; b = P[min(i + 1, len(P) - 1)]
        d = (b - a); d.z = 0
        if d.length < 1e-6: d = Vector((1, 0, 0))
        d.normalize(); n = Vector((-d.y, d.x, 0)) * (w / 2)
        for q in (p - n, p + n, p + n + Vector((0, 0, h)), p - n + Vector((0, 0, h))): verts.append(q)
    for i in range(len(P) - 1):
        a, b = i * 4, (i + 1) * 4
        for k in range(4):
            k2 = (k + 1) % 4
            faces.append((a + k, a + k2, b + k2, b + k))
    me = bpy.data.meshes.new('curb'); me.from_pydata(verts, [], faces); me.materials.append(curb_m)
    new_obj('Curb', me)
for c in D['curbs']: curb(c)

# ---------------------------------------------------------------- building materials
def facade_mat(name, kind):
    """UV: u = metres along wall, v = metres above grade."""
    m, nt, out = mat(name); L = nt.links
    uv = nt.nodes.new('ShaderNodeUVMap'); sep = nt.nodes.new('ShaderNodeSeparateXYZ'); L.new(uv.outputs['UV'], sep.inputs[0])
    def mod(a, b):
        n = nt.nodes.new('ShaderNodeMath'); n.operation = 'FLOORED_MODULO'; n.inputs[1].default_value = b
        L.new(a, n.inputs[0]); return n.outputs[0]
    def cmp(a, op, b):
        n = nt.nodes.new('ShaderNodeMath'); n.operation = op; n.inputs[1].default_value = b; L.new(a, n.inputs[0]); return n.outputs[0]
    def mul(a, b):
        n = nt.nodes.new('ShaderNodeMath'); n.operation = 'MULTIPLY'; L.new(a, n.inputs[0]); L.new(b, n.inputs[1]); return n.outputs[0]
    def mixs(fac, a, b):
        n = nt.nodes.new('ShaderNodeMixShader'); L.new(fac, n.inputs[0]); L.new(a.outputs[0], n.inputs[1]); L.new(b.outputs[0], n.inputs[2]); return n
    u, v = sep.outputs['X'], sep.outputs['Y']
    # materials
    dark = principled(nt, **{'Base Color': (0.045, 0.047, 0.05, 1), 'Roughness': 0.45, 'Metallic': 0.6})
    light = principled(nt, **{'Base Color': (0.62, 0.61, 0.57, 1), 'Roughness': 0.7})
    # wood: horizontal boards via wave texture on v
    wv = nt.nodes.new('ShaderNodeTexWave'); wv.wave_type = 'BANDS'; wv.bands_direction = 'Y'
    wv.inputs['Scale'].default_value = 1.0; wv.inputs['Distortion'].default_value = 3; wv.inputs['Detail'].default_value = 4
    cmb = nt.nodes.new('ShaderNodeCombineXYZ'); vs = nt.nodes.new('ShaderNodeMath'); vs.operation = 'MULTIPLY'; vs.inputs[1].default_value = 9.0
    L.new(v, vs.inputs[0]); L.new(u, cmb.inputs['X']); L.new(vs.outputs[0], cmb.inputs['Y']); L.new(cmb.outputs[0], wv.inputs['Vector'])
    wr = nt.nodes.new('ShaderNodeValToRGB'); wr.color_ramp.elements[0].color = (0.16, 0.075, 0.03, 1); wr.color_ramp.elements[1].color = (0.36, 0.19, 0.085, 1)
    L.new(wv.outputs['Fac'], wr.inputs['Fac'])
    wood = principled(nt, Roughness=0.6); L.new(wr.outputs['Color'], wood.inputs['Base Color'])
    glass = principled(nt, **{'Base Color': (0.02, 0.025, 0.03, 1), 'Roughness': 0.03, 'Metallic': 0.0})
    glass.inputs['Specular IOR Level'].default_value = 0.9
    glass.inputs['Emission Color'].default_value = (1.0, 0.7, 0.42, 1); glass.inputs['Emission Strength'].default_value = 0.0
    # interior glow pattern on lower glass
    gl_n = nt.nodes.new('ShaderNodeTexNoise'); gl_n.inputs['Scale'].default_value = 0.35
    L.new(cmb.outputs[0], gl_n.inputs['Vector'])
    gm = nt.nodes.new('ShaderNodeMath'); gm.operation = 'MULTIPLY'; gm.inputs[1].default_value = 1.6
    L.new(gl_n.outputs['Fac'], gm.inputs[0]); L.new(gm.outputs[0], glass.inputs['Emission Strength'])
    mull = principled(nt, **{'Base Color': (0.02, 0.02, 0.022, 1), 'Roughness': 0.35, 'Metallic': 0.8})
    if kind == 'store':
        is_mull = cmp(mod(u, 1.6), 'LESS_THAN', 0.06)
        is_tr = mul(cmp(v, 'GREATER_THAN', 2.55), cmp(v, 'LESS_THAN', 2.62))
        mn = nt.nodes.new('ShaderNodeMath'); mn.operation = 'MAXIMUM'; L.new(is_mull, mn.inputs[0]); L.new(is_tr, mn.inputs[1])
        gz = mul(cmp(v, 'GREATER_THAN', 0.45), cmp(v, 'LESS_THAN', 3.5))
        glazing = mixs(mn.outputs[0], glass, mull)
        upper = mixs(cmp(mod(u, 14.0), 'LESS_THAN', 7.0), dark, wood)
        sh = mixs(gz, upper, glazing)
    elif kind == 'entry':
        is_mull = cmp(mod(u, 1.5), 'LESS_THAN', 0.07)
        is_tr = cmp(mod(v, 2.4), 'LESS_THAN', 0.07)
        mn = nt.nodes.new('ShaderNodeMath'); mn.operation = 'MAXIMUM'; L.new(is_mull, mn.inputs[0]); L.new(is_tr, mn.inputs[1])
        gz = mul(cmp(v, 'GREATER_THAN', 0.3), cmp(v, 'LESS_THAN', 7.4))
        frame = mul(cmp(mod(u, 100.0), 'GREATER_THAN', 0.9), cmp(v, 'LESS_THAN', 99))
        sh = mixs(gz, wood, mixs(mn.outputs[0], glass, mull))
    else:  # side: light panels, dark base, clerestory band, wood accent bays
        reveal = mul(cmp(mod(u, 2.44), 'LESS_THAN', 0.025), cmp(v, 'GREATER_THAN', 0.6))
        panel = mixs(reveal, light, dark)
        cz = mul(cmp(v, 'GREATER_THAN', 4.1), cmp(v, 'LESS_THAN', 5.0))
        cw = cmp(mod(u, 6.1), 'LESS_THAN', 3.6)
        clere = mixs(mul(cz, cw), panel, mixs(cmp(mod(u, 1.2), 'LESS_THAN', 0.05), glass, mull))
        accent = mul(cmp(mod(u, 24.4), 'LESS_THAN', 4.9), cmp(v, 'GREATER_THAN', 0.6))
        withwood = mixs(accent, clere, wood)
        sh = mixs(cmp(v, 'LESS_THAN', 0.6), withwood, dark)
    finish(nt, sh, out); return m

M_STORE = facade_mat('Storefront', 'store')
M_ENTRY = facade_mat('Entry', 'entry')
M_SIDE = facade_mat('Side', 'side')
M_ROOF = simple_mat('Roof', (0.48, 0.48, 0.47), 0.75)
M_CAP = simple_mat('Coping', (0.03, 0.032, 0.035), 0.35, 0.7)
M_WOODSOFF = facade_mat('WoodCanopy', 'side')

def area(poly):
    return 0.5 * sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1] for i in range(len(poly)))

def offset(poly, d):  # inward offset for CCW polygon by d (miter)
    n = len(poly); res = []
    for i in range(n):
        p0, p1, p2 = Vector(poly[i - 1]), Vector(poly[i]), Vector(poly[(i + 1) % n])
        e1 = (p1 - p0).normalized(); e2 = (p2 - p1).normalized()
        n1 = Vector((-e1.y, e1.x)); n2 = Vector((-e2.y, e2.x))
        bis = (n1 + n2); bis.normalize()
        res.append(tuple(p1 + bis * (d / max(0.2, bis.dot(n1)))))
    return res

def prism(name, poly, z0, z1, wall_fn, top_mat, bottom=False):
    """wall_fn(edge_index, p_a, p_b, outward_normal) -> material"""
    if area(poly) < 0: poly = poly[::-1]
    me = bpy.data.meshes.new(name); bm = bmesh.new(); uvl = bm.loops.layers.uv.new('UVMap')
    mats = []
    def mi(m):
        if m not in mats: mats.append(m)
        return mats.index(m)
    n = len(poly)
    for i in range(n):
        a, b = Vector((*poly[i], 0)), Vector((*poly[(i + 1) % n], 0))
        d = (b - a); ln = d.length
        if ln < 1e-4: continue
        nrm = Vector((d.y, -d.x, 0)).normalized()
        vs = [bm.verts.new((a.x, a.y, z0)), bm.verts.new((b.x, b.y, z0)), bm.verts.new((b.x, b.y, z1)), bm.verts.new((a.x, a.y, z1))]
        f = bm.faces.new(vs); f.material_index = mi(wall_fn(i, a, b, nrm))
        for lp, (uu, vv) in zip(f.loops, [(0, z0), (ln, z0), (ln, z1), (0, z1)]): lp[uvl].uv = (uu, vv)
    top = bm.faces.new([bm.verts.new((p[0], p[1], z1)) for p in poly]); top.material_index = mi(top_mat)
    for lp in top.loops: lp[uvl].uv = (lp.vert.co.x, lp.vert.co.y)
    bm.to_mesh(me); bm.free()
    for m in mats: me.materials.append(m)
    o = new_obj(name, me); return o

def ring(name, outer, d, z0, z1, m):
    if area(outer) < 0: outer = outer[::-1]
    inner = offset(outer, d); n = len(outer)
    me = bpy.data.meshes.new(name); bm = bmesh.new()
    vo0 = [bm.verts.new((p[0], p[1], z0)) for p in outer]; vo1 = [bm.verts.new((p[0], p[1], z1)) for p in outer]
    vi0 = [bm.verts.new((p[0], p[1], z0)) for p in inner]; vi1 = [bm.verts.new((p[0], p[1], z1)) for p in inner]
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new([vo0[i], vo0[j], vo1[j], vo1[i]]); bm.faces.new([vi0[j], vi0[i], vi1[i], vi1[j]])
        bm.faces.new([vo1[i], vo1[j], vi1[j], vi1[i]])
    bm.to_mesh(me); bm.free(); me.materials.append(m)
    return new_obj(name, me)

def box_along(a, b, nrm, depth, z0, z1, m, name='Canopy'):
    a, b = Vector(a), Vector(b)
    pts = [a, b, b + nrm * depth, a + nrm * depth]
    return prism(name, [(p.x, p.y) for p in pts], z0, z1, lambda *_: m, m)

def building(poly, H, front_dirs, entry_poly=None, entry_H=None, canopy=True):
    poly = [tuple(p) for p in poly]
    if poly[0] == poly[-1]: poly = poly[:-1]
    fronts = [Vector(f).normalized() for f in front_dirs]
    def wall(i, a, b, nrm):
        if any(nrm.to_2d().dot(f) > 0.8 for f in fronts): return M_STORE
        return M_SIDE
    prism('Bldg', poly, 0, H - 0.75, wall, M_ROOF)
    ring('Parapet', poly, 0.3, H - 0.75, H, M_SIDE)
    ring('Cap', [tuple(p) for p in offset(poly, -0.05)], 0.4, H, H + 0.08, M_CAP)
    if canopy:
        P = poly if area(poly) > 0 else poly[::-1]
        for i in range(len(P)):
            a, b = Vector(P[i]), Vector(P[(i + 1) % len(P)]); d = b - a
            if d.length < 8: continue
            nrm = Vector((d.y, -d.x)).normalized()
            if any(nrm.dot(f) > 0.8 for f in fronts):
                t0, t1 = 0.12, 0.88
                box_along(a + d * t0, a + d * t1, nrm, 2.0, 3.7, 3.95, M_CAP)
    if entry_poly:
        ep = [tuple(p) for p in entry_poly]
        prism('Entry', ep, 0, entry_H - 0.75, lambda i, a, b, n: M_ENTRY if any(n.to_2d().dot(f) > 0.5 for f in fronts) else M_WOODSOFF, M_ROOF)
        ring('EntryParapet', ep, 0.3, entry_H - 0.75, entry_H, M_ENTRY)
        ring('EntryCap', [tuple(p) for p in offset(ep if area(ep) > 0 else ep[::-1], -0.06)], 0.42, entry_H, entry_H + 0.1, M_CAP)


for B in D['buildings']:
    poly = [tuple(p) for p in B['poly']]
    fronts = [Vector(f) for f in B['fronts']]
    building(poly, B['H'], fronts)
    if B.get('entry'):
        ep = [tuple(p) for p in B['entry']]
        eh = B.get('entry_H', B['H'] + 2.2)
        fr2 = [f.to_2d() if hasattr(f, 'to_2d') else f for f in fronts]
        prism('Entry', ep, 0, eh - 0.75, lambda i, a, b, n: M_ENTRY if any(n.to_2d().dot(f.to_2d()) > 0.5 for f in fronts) else M_WOODSOFF, M_ROOF)
        ring('EntryParapet', ep, 0.3, eh - 0.75, eh, M_WOODSOFF)
        ring('EntryCap', [tuple(p) for p in offset(ep if area(ep) > 0 else ep[::-1], -0.06)], 0.42, eh, eh + 0.1, M_CAP)

for B in D.get('existing', []):   # existing buildings that stay: plain massing, the real roof/wall materials
    prism('Existing', [tuple(p) for p in B['poly']], 0, B['H'], lambda *a: M_SIDE, M_ROOF)

# ---------------------------------------------------------------- pond
if D.get('pond'):
    wm = bpy.data.materials.new('Water'); wm.use_nodes = True; wnt = wm.node_tree; wnt.nodes.clear()
    wout = wnt.nodes.new('ShaderNodeOutputMaterial')
    wp = principled(wnt, **{'Base Color': (0.03, 0.06, 0.06, 1), 'Roughness': 0.05})
    finish(wnt, wp, wout)
    for ringpts in D['pond'][:1]:
        me = bpy.data.meshes.new('pond'); bm = bmesh.new()
        vs = [bm.verts.new((p[0], p[1], 0.03)) for p in ringpts[:-1]]
        bm.faces.new(vs); bm.to_mesh(me); bm.free(); me.materials.append(wm); new_obj('Pond', me)


# ---------------------------------------------------------------- trees
tc_coll = bpy.data.collections.new('TreeProtos'); sc.collection.children.link(tc_coll)
lay = bpy.context.view_layer.layer_collection.children['TreeProtos']; lay.exclude = False
leaf_m, nt, out = mat('Leaves')
oi = nt.nodes.new('ShaderNodeObjectInfo')
ramp = nt.nodes.new('ShaderNodeValToRGB')
el = ramp.color_ramp.elements
el[0].position = 0.0; el[0].color = (0.012, 0.03, 0.008, 1)
el[1].position = 1.0; el[1].color = (0.07, 0.11, 0.022, 1)
e2 = el.new(0.55); e2.color = (0.03, 0.065, 0.014, 1)
e3 = el.new(0.85); e3.color = (0.075, 0.09, 0.02, 1)
vor = nt.nodes.new('ShaderNodeTexVoronoi'); vor.inputs['Scale'].default_value = 3.5
tcl = nt.nodes.new('ShaderNodeTexCoord')
nt.links.new(tcl.outputs['Object'], vor.inputs['Vector'])
mixc = nt.nodes.new('ShaderNodeMath'); mixc.operation = 'MULTIPLY_ADD'; mixc.inputs[1].default_value = 0.25
nt.links.new(vor.outputs['Distance'], mixc.inputs[0]); nt.links.new(oi.outputs['Random'], mixc.inputs[2])
nt.links.new(mixc.outputs[0], ramp.inputs['Fac'])
ao = nt.nodes.new('ShaderNodeAmbientOcclusion'); ao.inputs['Distance'].default_value = 0.9; ao.only_local = True; ao.samples = 4
aor = nt.nodes.new('ShaderNodeMapRange'); aor.inputs['To Min'].default_value = 0.25
nt.links.new(ao.outputs['AO'], aor.inputs['Value'])
aom = nt.nodes.new('ShaderNodeMix'); aom.data_type = 'RGBA'; aom.blend_type = 'MULTIPLY'; aom.inputs['Factor'].default_value = 1.0
nt.links.new(ramp.outputs['Color'], aom.inputs[6])
aoc = nt.nodes.new('ShaderNodeCombineColor'); nt.links.new(aor.outputs['Result'], aoc.inputs[0]); nt.links.new(aor.outputs['Result'], aoc.inputs[1]); nt.links.new(aor.outputs['Result'], aoc.inputs[2])
nt.links.new(aoc.outputs[0], aom.inputs[7])
pl = principled(nt, Roughness=0.65)
pl.inputs['Subsurface Weight'].default_value = 0.0
nt.links.new(aom.outputs[2], pl.inputs['Base Color'])
bmp = nt.nodes.new('ShaderNodeBump'); bmp.inputs['Strength'].default_value = 0.6; bmp.inputs['Distance'].default_value = 0.3
vor2 = nt.nodes.new('ShaderNodeTexVoronoi'); vor2.inputs['Scale'].default_value = 7
nt.links.new(tcl.outputs['Object'], vor2.inputs['Vector']); nt.links.new(vor2.outputs['Distance'], bmp.inputs['Height'])
nt.links.new(bmp.outputs['Normal'], pl.inputs['Normal'])
tr = nt.nodes.new('ShaderNodeBsdfTranslucent'); nt.links.new(ramp.outputs['Color'], tr.inputs['Color'])
lm = nt.nodes.new('ShaderNodeMixShader'); lm.inputs['Fac'].default_value = 0.18
nt.links.new(pl.outputs[0], lm.inputs[1]); nt.links.new(tr.outputs[0], lm.inputs[2])
finish(nt, lm, out)
bark_m = simple_mat('Bark', (0.06, 0.05, 0.04), 0.9)

cloud = bpy.data.textures.new('lumps', 'CLOUDS'); cloud.noise_scale = 0.22; cloud.noise_depth = 2
def make_tree(i):
    r = random.Random(100 + i)
    bm = bmesh.new()
    conifer = i >= 6
    if conifer:
        for k in range(7):
            z = -0.6 + k * 0.28; rad = 0.62 * (1 - k / 7.5) + 0.08
            res = bmesh.ops.create_cone(bm, cap_ends=True, segments=10, radius1=rad, radius2=rad * 0.25, depth=0.45)
            bmesh.ops.translate(bm, verts=res['verts'], vec=Vector((r.uniform(-.04, .04), r.uniform(-.04, .04), z)))
    else:
        nl = r.randint(18, 28)
        for k in range(nl):
            rad = r.uniform(0.22, 0.42)
            ang = r.uniform(0, 2 * math.pi); dist = r.uniform(0.0, 0.62) ** 0.8
            zc = r.uniform(-0.3, 0.42) * (1 - 0.4 * dist)
            res = bmesh.ops.create_icosphere(bm, subdivisions=2, radius=rad)
            bmesh.ops.translate(bm, verts=res['verts'], vec=Vector((math.cos(ang) * dist, math.sin(ang) * dist, zc)))
    raw = bpy.data.meshes.new(f'raw{i}'); bm.to_mesh(raw); bm.free()
    sz = r.uniform(0.75, 0.95)
    for v in raw.vertices: v.co.z *= sz
    tmp = new_obj(f'tmp{i}', raw, tc_coll)
    md = tmp.modifiers.new('d', 'DISPLACE'); md.texture = cloud; md.strength = 0.14; md.texture_coords = 'LOCAL'
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(tmp.evaluated_get(dg))
    bpy.data.objects.remove(tmp); bpy.data.meshes.remove(raw)
    for p in me.polygons: p.use_smooth = True
    me.materials.append(leaf_m)
    # trunk joined in
    tb = bmesh.new(); tb.from_mesh(me)
    cyl = bmesh.ops.create_cone(tb, cap_ends=True, segments=8, radius1=0.07, radius2=0.04, depth=1.6)
    bmesh.ops.translate(tb, verts=cyl['verts'], vec=Vector((0, 0, -1.0)))
    for f in tb.faces:
        if all(v in cyl['verts'] for v in f.verts): f.material_index = 1
    tb.to_mesh(me); tb.free(); me.materials.append(bark_m)
    # origin at ground: crown centre sits ~1.8 units up
    for v in me.vertices: v.co.z += 1.8
    o = new_obj(f'tree{i}', me, tc_coll)
    return o
protos = [make_tree(i) for i in range(8)]

# point cloud with per-tree scale
pts = D['trees']
me = bpy.data.meshes.new('treepts')
me.from_pydata([(p[0] + rnd.uniform(-1.5, 1.5), p[1] + rnd.uniform(-1.5, 1.5), 0) for p in pts] + [(o[0], o[1], 0) for o in D.get('ornamental', [])], [], [])
att = me.attributes.new('scl', 'FLOAT', 'POINT')
att.data.foreach_set('value', [p[5] for p in pts] + [o[2] for o in D.get('ornamental', [])])
forest = new_obj('Forest', me)
ng = bpy.data.node_groups.new('Scatter', 'GeometryNodeTree')
ng.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
ng.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
N = ng.nodes; Lk = ng.links
gi = N.new('NodeGroupInput'); go = N.new('NodeGroupOutput')
iop = N.new('GeometryNodeInstanceOnPoints')
ci = N.new('GeometryNodeCollectionInfo'); ci.inputs['Collection'].default_value = tc_coll
ci.inputs['Separate Children'].default_value = True; ci.inputs['Reset Children'].default_value = True
rv = N.new('FunctionNodeRandomValue'); rv.data_type = 'INT'
for sk in rv.inputs:
    if sk.type == 'INT' and sk.name == 'Min': sk.default_value = 0
    if sk.type == 'INT' and sk.name == 'Max': sk.default_value = len(protos) - 1
rv_out = [sk for sk in rv.outputs if sk.type == 'INT'][0]
rr = N.new('FunctionNodeRandomValue'); rr.data_type = 'FLOAT_VECTOR'
for sk in rr.inputs:
    if sk.type == 'VECTOR' and sk.name == 'Min': sk.default_value = (0, 0, 0)
    if sk.type == 'VECTOR' and sk.name == 'Max': sk.default_value = (0, 0, 6.283)
rr_out = [sk for sk in rr.outputs if sk.type == 'VECTOR'][0]
na = N.new('GeometryNodeInputNamedAttribute'); na.data_type = 'FLOAT'; na.inputs['Name'].default_value = 'scl'
Lk.new(gi.outputs[0], iop.inputs['Points']); Lk.new(ci.outputs[0], iop.inputs['Instance'])
iop.inputs['Pick Instance'].default_value = True
Lk.new(rv_out, iop.inputs['Instance Index'])
Lk.new(rr_out, iop.inputs['Rotation']); Lk.new(na.outputs[0], iop.inputs['Scale'])
Lk.new(iop.outputs[0], go.inputs[0])
gm = forest.modifiers.new('scatter', 'NODES'); gm.node_group = ng
lay.exclude = True

# ---------------------------------------------------------------- world + sun
world = bpy.data.worlds.new('W'); sc.world = world; world.use_nodes = True
wn = world.node_tree; wn.nodes.clear()
sky = wn.nodes.new('ShaderNodeTexSky')
SUN_EL, SUN_AZ = math.radians(D['sun']['el_deg']), math.radians(D['sun']['az_deg'])
for t in ('MULTIPLE_SCATTERING', 'SINGLE_SCATTERING', 'NISHITA'):
    try: sky.sky_type = t; break
    except Exception: pass
try: sky.sun_disc = False
except Exception: pass
sky.sun_elevation = SUN_EL; sky.sun_rotation = math.radians(90) - SUN_AZ + math.pi
try: sky.altitude = 300
except Exception: pass
try: sky.air_density = 1.4; sky.dust_density = 2.5
except Exception: pass
bg = wn.nodes.new('ShaderNodeBackground'); bg.inputs['Strength'].default_value = 0.22
wo = wn.nodes.new('ShaderNodeOutputWorld')
wn.links.new(sky.outputs[0], bg.inputs[0]); wn.links.new(bg.outputs[0], wo.inputs[0])
sun = bpy.data.lights.new('Sun', 'SUN'); sun.energy = 4.2; sun.angle = math.radians(0.8); sun.color = (1.0, 0.86, 0.68)
so = new_obj('Sun', sun)
dirv = Vector((math.cos(SUN_EL) * math.sin(SUN_AZ), math.cos(SUN_EL) * math.cos(SUN_AZ), math.sin(SUN_EL)))
so.rotation_euler = (-dirv).to_track_quat('-Z', 'Y').to_euler()

# ---------------------------------------------------------------- camera
CAM = D['camera']
pos, tgt, lens = CAM['pos'], CAM['target'], CAM['lens']
cam = bpy.data.cameras.new('Cam'); cam.lens = lens; cam.clip_end = 8000; cam.sensor_width = 36
co = new_obj('Cam', cam); co.location = pos
co.rotation_euler = (Vector(tgt) - Vector(pos)).to_track_quat('-Z', 'Y').to_euler()
sc.camera = co


# ---------------------------------------------------------------- render
sc.render.engine = 'CYCLES'
sc.cycles.device = 'CPU'
sc.cycles.samples = SPP; sc.cycles.use_adaptive_sampling = True; sc.cycles.adaptive_threshold = 0.03
sc.cycles.use_denoising = True
try: sc.cycles.denoiser = 'OPENIMAGEDENOISE'
except Exception: pass
sc.cycles.max_bounces = 4; sc.cycles.diffuse_bounces = 2; sc.cycles.glossy_bounces = 2; sc.cycles.transmission_bounces = 2
sc.cycles.transparent_max_bounces = 4
sc.cycles.caustics_reflective = False; sc.cycles.caustics_refractive = False
sc.render.resolution_x = RW; sc.render.resolution_y = RH; sc.render.resolution_percentage = 100
sc.render.threads_mode = 'AUTO'
try:
    sc.view_settings.view_transform = 'AgX'; sc.view_settings.look = 'AgX - Medium High Contrast'
except Exception: pass
sc.view_settings.exposure = 0.0
sc.render.image_settings.file_format = 'PNG'
sc.render.filepath = OUT
bpy.ops.render.render(write_still=True)
print('DONE', OUT)

# ---------------------------------------------------------------- lot mask (screen-space polygons of what may change)
from bpy_extras.object_utils import world_to_camera_view
def proj(x, y, z):
    v = world_to_camera_view(sc, co, Vector((x, y, z)))
    return [round(v.x * RW, 1), round((1 - v.y) * RH, 1)]
polys = []
for ring_ in D.get('lot', [])[:1]:
    polys.append([proj(p[0], p[1], 0.0) for p in ring_])
for B in D['buildings']:
    top = B.get('entry_H', B['H']) + 0.3
    polys.append([proj(p[0], p[1], 0.0) for p in B['poly']] + [proj(p[0], p[1], top) for p in B['poly']])   # hull later
json.dump({'w': RW, 'h': RH, 'lot': polys[:1], 'buildings': polys[1:]}, open(OUT.replace('.png', '_mask.json'), 'w'))
print('MASK', OUT.replace('.png', '_mask.json'))
