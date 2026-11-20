using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text.Json;
using System.Text.RegularExpressions;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;
using Autodesk.Civil.DatabaseServices.Styles;
using AcEntity = Autodesk.AutoCAD.DatabaseServices.Entity;

namespace Drafted.C3D
{
    /// <summary>What to build and with which of the drawing's styles / parts lists / layers (chosen in the import dialog).</summary>
    internal class Options
    {
        public bool Surfaces = true, Networks = true, Ponds = true, Features = true, Alignments = true, Points = true, Linework = true;
        public Dictionary<string, string> Styles = new Dictionary<string, string>();   // key -> style name in this drawing
        public Dictionary<string, string> Layers = new Dictionary<string, string>();   // Drafted standard layer -> drawing layer
        public string Site = "Drafted";
    }

    /// <summary>Style / parts list catalogues of the open drawing (names), read once for the dialog and for name -> id lookups.</summary>
    internal class Catalog
    {
        public readonly Dictionary<string, Dictionary<string, ObjectId>> Sets = new Dictionary<string, Dictionary<string, ObjectId>>();
        public readonly List<string> Layers = new List<string>();

        // dialog key -> path below CivilDocument (read by reflection so a collection missing from a release simply shows empty)
        public static readonly (string Key, string Label, string Path, string[] Prefer)[] Kinds =
        {
            ("surface_eg", "EG surface style", "Styles.SurfaceStyles", new[] { "Exist", "EG", "Border & Contours", "Contours" }),
            ("surface_fg", "FG surface style", "Styles.SurfaceStyles", new[] { "Finish", "Propos", "Design", "FG", "Contours" }),
            ("parts_storm", "Storm parts list", "Styles.PartsListSet", new[] { "Storm", "Drain", "SD" }),
            ("parts_sanitary", "Sanitary parts list", "Styles.PartsListSet", new[] { "Sanitary", "Sewer", "SAN", "SS" }),
            ("pipe_style", "Pipe style", "Styles.PipeStyles", new[] { "Single Line", "Double Line", "Standard" }),
            ("struct_style", "Structure style", "Styles.StructureStyles", new[] { "Standard", "Plan" }),
            ("pipe_rules", "Pipe rule set", "Styles.PipeRuleSet", new[] { "Basic", "Standard" }),
            ("struct_rules", "Structure rule set", "Styles.StructureRuleSet", new[] { "Basic", "Standard" }),
            ("pond_style", "Pond style", "Styles.PondStyles", new[] { "Standard", "Pond" }),
            ("align_style", "Alignment style", "Styles.AlignmentStyles", new[] { "Proposed", "Design", "Standard" }),
            ("align_labels", "Alignment label set", "Styles.LabelSetStyles.AlignmentLabelSetStyles", new[] { "Major", "Standard" }),
            ("profile_style", "Design profile style", "Styles.ProfileStyles", new[] { "Design", "Proposed", "Finish", "Standard" }),
            ("profile_eg_style", "EG profile style", "Styles.ProfileStyles", new[] { "Exist", "EG", "Standard" }),
            ("profile_labels", "Profile label set", "Styles.LabelSetStyles.ProfileLabelSetStyles", new[] { "Complete", "Standard" }),
            ("feature_style", "Feature line style", "Styles.FeatureLineStyles", new[] { "Grading", "Standard" }),
            ("point_style", "Point style", "Styles.PointStyles", new[] { "Spot", "Elev", "Standard" }),
            ("point_label", "Point label style", "Styles.LabelStyles.PointLabelStyles.LabelStyles", new[] { "Elevation", "Elev", "Standard" }),
        };

        public Catalog(CivilDocument civ, Database db, Transaction tr)
        {
            foreach (var k in Kinds)
            {
                if (Sets.ContainsKey(k.Path)) continue;
                var d = new Dictionary<string, ObjectId>(StringComparer.OrdinalIgnoreCase);
                object o = civ;
                foreach (var part in k.Path.Split('.')) { o = o == null ? null : U.TryGet(o, part); }
                if (o is IEnumerable en)
                    foreach (var x in en)
                    {
                        if (!(x is ObjectId id) || id.IsNull) continue;
                        try { var dbo = tr.GetObject(id, OpenMode.ForRead); var n = U.TryGet(dbo, "Name") as string; if (!string.IsNullOrEmpty(n) && !d.ContainsKey(n)) d[n] = id; }
                        catch { }
                    }
                if (d.Count == 0 && k.Path == "Styles.PartsListSet")
                    try
                    {
                        foreach (ObjectId id in civ.Styles.PartsListSet)
                        {
                            var n = (tr.GetObject(id, OpenMode.ForRead) as PartsList)?.Name;
                            if (!string.IsNullOrEmpty(n) && !d.ContainsKey(n)) d[n] = id;
                        }
                    }
                    catch { }
                Sets[k.Path] = d;
            }
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            foreach (ObjectId id in lt)
            {
                var r = (LayerTableRecord)tr.GetObject(id, OpenMode.ForRead);
                if (!r.Name.Contains("|")) Layers.Add(r.Name);
            }
            Layers.Sort(StringComparer.OrdinalIgnoreCase);
        }

        /// <summary>Any parts list in the drawing (when none was chosen in the dialog).</summary>
        public ObjectId FirstPartsList()
        {
            var set = Sets["Styles.PartsListSet"];
            if (set.Count > 0) return set.Values.First();
            try { foreach (ObjectId id in CivilApplication.ActiveDocument.Styles.PartsListSet) return id; } catch { }
            return ObjectId.Null;
        }

        public List<string> Names(string key) => Sets[Kinds.First(k => k.Key == key).Path].Keys.OrderBy(n => n).ToList();

        public ObjectId Id(string key, Options o)
        {
            if (!o.Styles.TryGetValue(key, out var n) || string.IsNullOrEmpty(n)) return ObjectId.Null;
            var set = Sets[Kinds.First(k => k.Key == key).Path];
            return set.TryGetValue(n, out var id) ? id : ObjectId.Null;
        }
    }

    internal class Importer
    {
        readonly Document doc;
        readonly Database db;
        readonly Editor ed;
        readonly CivilDocument civ;
        readonly JsonElement M;
        readonly string dir;
        readonly Options O;
        readonly List<string> report = new List<string>();
        readonly List<string> warn = new List<string>();
        readonly Dictionary<string, ObjectId> pipeByName = new Dictionary<string, ObjectId>();
        ObjectId fgId = ObjectId.Null, egId = ObjectId.Null;

        public Importer(Document doc, JsonElement manifest, string dir, Options o)
        {
            this.doc = doc; db = doc.Database; ed = doc.Editor; civ = CivilApplication.ActiveDocument; M = manifest; this.dir = dir; O = o;
        }

        string Layer(string std)
        {
            var name = O.Layers.TryGetValue(std, out var n) && !string.IsNullOrEmpty(n) ? n : std;
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                if (!lt.Has(name))
                {
                    lt.UpgradeOpen();
                    var r = new LayerTableRecord { Name = name };
                    lt.Add(r);
                    tr.AddNewlyCreatedDBObject(r, true);
                }
                tr.Commit();
            }
            return name;
        }

        ObjectId LayerId(string std)
        {
            var name = Layer(std);
            using (var tr = db.TransactionManager.StartOpenCloseTransaction())
            {
                var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                return lt[name];
            }
        }

        public List<string> Run(Catalog cat)
        {
            using (doc.LockDocument())
            {
                if (O.Surfaces) Step("Surfaces", () => Surfaces(cat));
                if (O.Networks) Step("Pipe networks", () => Networks(cat));
                if (O.Ponds) Step("Ponds", () => Ponds(cat));
                if (O.Features) Step("Feature lines", () => Features(cat));
                if (O.Alignments) Step("Alignments", () => Alignments(cat));
                if (O.Points) Step("COGO points", () => Points(cat));
                if (O.Linework) Step("Linework", () => Linework());
            }
            return report.Concat(warn.Select(w => "  ! " + w)).ToList();
        }

        void Step(string what, Action a)
        {
            try { ed.WriteMessage($"\nDrafted: {what}..."); a(); }
            catch (System.Exception e) { warn.Add($"{what} failed: {e.Message}"); }
        }

        // ------------------------------------------------------------------ surfaces
        void Surfaces(Catalog cat)
        {
            foreach (var s in U.A(M, "surfaces"))
            {
                var name = U.S(s, "name");
                var key = name == "EG" ? "surface_eg" : "surface_fg";
                var pts = U.A(s, "points").Select(x => x.GetDouble()).ToArray();
                var pc = new Point3dCollection();
                for (int i = 0; i + 2 < pts.Length; i += 3) pc.Add(new Point3d(pts[i], pts[i + 1], pts[i + 2]));
                using (var tr = db.TransactionManager.StartTransaction())
                {
                    var sname = Unique(name, n => SurfaceNamed(tr, n));
                    var id = TinSurface.Create(db, sname);
                    var t = (TinSurface)tr.GetObject(id, OpenMode.ForWrite);
                    t.Description = U.S(s, "desc");
                    t.Layer = Layer(U.S(s, "layer", name == "EG" ? "V-TOPO" : "C-TOPO"));
                    var st = cat.Id(key, O);
                    if (!st.IsNull) t.StyleId = st;
                    t.AddVertices(pc);
                    if (s.TryGetProperty("boundary", out var b) && b.ValueKind == JsonValueKind.Array)
                    {
                        var pl = Poly2(b, 0, true);
                        var ms = ModelSpace(tr);
                        pl.Layer = t.Layer;
                        var plId = ms.AppendEntity(pl);
                        tr.AddNewlyCreatedDBObject(pl, true);
                        t.BoundariesDefinition.AddBoundaries(new ObjectIdCollection { plId }, 0.1, SurfaceBoundaryType.Outer, true);
                        t.Rebuild();
                        pl.Erase();
                    }
                    if (name == "EG") egId = id; else fgId = id;
                    tr.Commit();
                    report.Add($"Surface {sname}: {pc.Count:N0} points");
                }
            }
        }

        static bool SurfaceNamed(Transaction tr, string n)
        {
            foreach (ObjectId id in CivilApplication.ActiveDocument.GetSurfaceIds())
                if (((Autodesk.Civil.DatabaseServices.Surface)tr.GetObject(id, OpenMode.ForRead)).Name == n) return true;
            return false;
        }

        static string Unique(string n, Func<string, bool> taken)
        {
            if (!taken(n)) return n;
            for (int i = 2; ; i++) if (!taken($"{n} ({i})")) return $"{n} ({i})";
        }

        // ------------------------------------------------------------------ gravity pipe networks on the firm's parts lists
        void Networks(Catalog cat)
        {
            var nets = U.A(M, "networks").ToList();
            if (nets.Count == 0) { report.Add("Pipe networks: none in this package"); return; }
            foreach (var n in nets)
            {
                var kind = U.S(n, "kind");
                string netName = U.S(n, "name");
                var key = kind == "sanitary" ? "parts_sanitary" : "parts_storm";
                var plId = cat.Id(key, O);
                if (plId.IsNull) plId = cat.FirstPartsList();
                if (plId.IsNull) { warn.Add($"{netName} network skipped: this drawing has no parts lists (Settings > Pipe Network > Parts Lists)"); continue; }
                int ns = 0, np = 0, bad = 0;
                string plName = "";
                ObjectId netId = ObjectId.Null;
                try
                {
                    using (var tr = db.TransactionManager.StartTransaction())
                    {
                        netId = Network.Create(civ, ref netName);
                        var net = (Network)tr.GetObject(netId, OpenMode.ForWrite);
                        net.PartsListId = plId;
                        tr.Commit();
                    }
                }
                catch (System.Exception e) { warn.Add($"{netName}: could not create the network ({e.GetType().Name}: {e.Message})"); continue; }
                using (var tr = db.TransactionManager.StartTransaction())
                {
                    var net = (Network)tr.GetObject(netId, OpenMode.ForWrite);
                    if (!fgId.IsNull) U.TrySet(net, "ReferenceSurfaceId", fgId);
                    var plist = (PartsList)tr.GetObject(plId, OpenMode.ForRead);
                    plName = plist.Name;
                    var pipeFams = Families(tr, plist, DomainType.Pipe);
                    var strFams = Families(tr, plist, DomainType.Structure);
                    if (pipeFams.Count == 0 || strFams.Count == 0)
                    {
                        warn.Add($"{netName}: parts list \"{plName}\" has {pipeFams.Count} pipe and {strFams.Count} structure families with sizes; add sizes to it");
                        tr.Commit(); continue;
                    }
                    string pLayer = Layer(U.S(n, "pipe_layer", "C-STRM-PIPE")), sLayer = Layer(U.S(n, "struct_layer", "C-STRM-STRC"));
                    var pStyle = cat.Id("pipe_style", O); var sStyle = cat.Id("struct_style", O);
                    var sids = new Dictionary<string, ObjectId>();
                    foreach (var s in U.A(n, "structures"))
                    {
                        var sname = U.S(s, "name");
                        try
                        {
                            var k = U.S(s, "kind");
                            var fam = PickFamily(strFams, k == "CB" ? new[] { "Catch", "Inlet", "Grate", "Curb", "Rect" }
                                                         : k == "FES" ? new[] { "Flared", "End", "Headwall", "Null", "Outfall" }
                                                         : k == "CO" ? new[] { "Cleanout", "Clean Out", "Null", "Cylind", "Circular" }
                                                         : k == "TAP" ? new[] { "Null", "Wye", "Tee", "Junction", "Connection" }
                                                         : new[] { "Manhole", "Concentric", "Eccentric", "Cylind", "Circular" });
                            var size = PickSize(tr, fam.Sizes, U.D(s, "dia_in", 48), false);
                            var xy = U.XY(s.GetProperty("xy"));
                            var rim = U.D(s, "rim");
                            ObjectId sid = ObjectId.Null;
                            net.AddStructure(fam.Id, size, new Point3d(xy[0], xy[1], rim), 0.0, ref sid, false);
                            if (sid.IsNull) { bad++; warn.Add($"{netName} {sname}: Civil 3D did not create the structure"); continue; }
                            var st = (Structure)tr.GetObject(sid, OpenMode.ForWrite);
                            U.TrySet(st, "Name", sname);
                            U.TrySet(st, "Description", U.S(s, "desc"));
                            try { st.Layer = sLayer; } catch { }
                            if (!sStyle.IsNull) try { st.StyleId = sStyle; } catch { }
                            U.TrySet(st, "RimElevation", rim);
                            sids[sname] = sid; ns++;
                        }
                        catch (System.Exception e) { bad++; warn.Add($"{netName} {sname}: {e.GetType().Name}: {e.Message}"); }
                    }
                    foreach (var p in U.A(n, "pipes"))
                    {
                        var pname = U.S(p, "name");
                        try
                        {
                            var fam = PickFamily(pipeFams, MaterialWords(U.S(p, "material")));
                            double dia;
                            var size = PickSize(tr, fam.Sizes, U.D(p, "dia_in", 12), true, out dia);
                            var a = U.XY(p.GetProperty("start")); var b = U.XY(p.GetProperty("end"));
                            double r = (dia > 0 ? dia : U.D(p, "dia_in", 12) / 12.0) / 2.0;      // pipe geometry is its centreline
                            var seg = new LineSegment3d(new Point3d(a[0], a[1], U.D(p, "inv_up") + r), new Point3d(b[0], b[1], U.D(p, "inv_dn") + r));
                            ObjectId pid = ObjectId.Null;
                            net.AddLinePipe(fam.Id, size, seg, ref pid, false);
                            if (pid.IsNull) { bad++; warn.Add($"{netName} {pname}: Civil 3D did not create the pipe"); continue; }
                            var pipe = (Pipe)tr.GetObject(pid, OpenMode.ForWrite);
                            U.TrySet(pipe, "Name", pname);
                            U.TrySet(pipe, "Description", $"{U.D(p, "dia_in"):0}\" {U.S(p, "material")} @ {U.D(p, "slope") * 100:0.00}%");
                            try { pipe.Layer = pLayer; } catch { }
                            if (!pStyle.IsNull) try { pipe.StyleId = pStyle; } catch { }
                            try { if (sids.TryGetValue(U.S(p, "from"), out var s0)) pipe.ConnectToStructure(ConnectorPositionType.Start, s0, true); }
                            catch (System.Exception e) { warn.Add($"{netName} {pname}: not connected to {U.S(p, "from")} ({e.Message})"); }
                            try { if (sids.TryGetValue(U.S(p, "to"), out var s1)) pipe.ConnectToStructure(ConnectorPositionType.End, s1, true); }
                            catch (System.Exception e) { warn.Add($"{netName} {pname}: not connected to {U.S(p, "to")} ({e.Message})"); }
                            pipeByName[pname] = pid; np++;
                        }
                        catch (System.Exception e) { bad++; warn.Add($"{netName} {pname}: {e.GetType().Name}: {e.Message}"); }
                    }
                    tr.Commit();
                }
                report.Add($"Pipe network {netName}: {ns} structures, {np} pipes on parts list \"{plName}\"{(bad > 0 ? $" ({bad} failed, see below)" : "")}");
            }
        }

        class Fam { public ObjectId Id; public string Name; public List<ObjectId> Sizes = new List<ObjectId>(); }

        static List<Fam> Families(Transaction tr, PartsList pl, DomainType dom)
        {
            var o = new List<Fam>();
            foreach (ObjectId id in pl.GetPartFamilyIdsByDomain(dom))
            {
                var f = (PartFamily)tr.GetObject(id, OpenMode.ForRead);
                var fam = new Fam { Id = id, Name = f.Name };
                for (int i = 0; i < f.PartSizeCount; i++) fam.Sizes.Add(f[i]);
                if (fam.Sizes.Count > 0) o.Add(fam);
            }
            return o;
        }

        static Fam PickFamily(List<Fam> fams, string[] words)
        {
            foreach (var w in words)
            {
                var f = fams.FirstOrDefault(x => x.Name.IndexOf(w, StringComparison.OrdinalIgnoreCase) >= 0);
                if (f != null) return f;
            }
            return fams[0];
        }

        static string[] MaterialWords(string mat)
        {
            var m = (mat ?? "").ToUpperInvariant();
            if (m.Contains("HDPE")) return new[] { "HDPE", "Polyethylene", "Corrugated", "Plastic", "PVC" };
            if (m.Contains("PVC")) return new[] { "PVC", "Plastic", "Polyvinyl", "HDPE" };
            if (m.Contains("DI") || m.Contains("DUCTILE")) return new[] { "Ductile", "DI", "Iron" };
            return new[] { "Concrete", "RCP", "Circular" };
        }

        static ObjectId PickSize(Transaction tr, List<ObjectId> sizes, double inches, bool pipe) => PickSize(tr, sizes, inches, pipe, out _);

        /// <summary>The part size closest to `inches` (inner diameter for pipes, diameter for structures). dia_ft = its size in feet.</summary>
        static ObjectId PickSize(Transaction tr, List<ObjectId> sizes, double inches, bool pipe, out double diaFt)
        {
            ObjectId best = sizes[0]; double bd = double.MaxValue; diaFt = 0;
            foreach (var id in sizes)
            {
                var ps = (PartSize)tr.GetObject(id, OpenMode.ForRead);
                double v = SizeValue(ps, pipe);
                if (v <= 0) continue;
                double vin = v > 6 ? v : v * 12.0;           // part data is in inches or in feet depending on the catalogue
                double d = Math.Abs(vin - inches) + (vin < inches ? 0.01 : 0);
                if (d < bd) { bd = d; best = id; diaFt = vin / 12.0; }
            }
            return best;
        }

        static double SizeValue(PartSize ps, bool pipe)
        {
            try
            {
                var rec = U.TryGet(ps, "SizeDataRecord");
                if (rec != null)
                {
                    var (ok, fields) = U.TryCall(rec, "GetAllDataFields");
                    if (ok && fields is IEnumerable en)
                        foreach (var f in en)
                        {
                            var ctx = U.TryGet(f, "Context")?.ToString() ?? "";
                            if (pipe ? ctx.Contains("PipeInnerDiameter") || ctx.Contains("PipeInnerWidth") : ctx.Contains("StructInnerDiameter") || ctx.Contains("StructDiameter") || ctx.Contains("StructInnerWidth"))
                            {
                                var val = U.TryGet(f, "Value");
                                if (val != null && double.TryParse(val.ToString(), out var dv) && dv > 0) return dv;
                            }
                        }
                }
            }
            catch { }
            var m = Regex.Match(ps.Name ?? "", @"(\d+(\.\d+)?)");
            return m.Success ? double.Parse(m.Groups[1].Value) : 0;
        }

        // ------------------------------------------------------------------ native ponds (Civil 3D 2027 pond objects)
        static Type FindType(string name)
        {
            foreach (var asmName in new[] { "AeccDrainageDesignMgd", "AeccDbMgd" })
                try { System.Reflection.Assembly.Load(asmName); } catch { }
            return AppDomain.CurrentDomain.GetAssemblies().Select(a => { try { return a.GetType(name); } catch { return null; } }).FirstOrDefault(t => t != null);
        }

        void Ponds(Catalog cat)
        {
            var pondT = FindType("Autodesk.Civil.DatabaseServices.Pond");
            var contT = FindType("Autodesk.Civil.DatabaseServices.PondContour");
            var collT = FindType("Autodesk.Civil.DatabaseServices.PondContourCollection");
            bool native = pondT != null && contT != null && collT != null;
            if (!native) warn.Add("This Civil 3D has no pond objects (2027 or later needed): ponds were drawn as feature lines");
            foreach (var p in U.A(M, "ponds"))
            {
                var name = U.S(p, "name", "POND");
                var layer = Layer(U.S(p, "layer", "C-STRM-POND"));
                var cons = U.A(p, "contours").ToList();
                if (cons.Count < 2) continue;
                if (native)
                {
                    try
                    {
                        using (var tr = db.TransactionManager.StartTransaction())
                        {
                            var coll = Activator.CreateInstance(collT);
                            var add = collT.GetMethod("Add", new[] { contT });
                            foreach (var c in cons)
                            {
                                var pl = Poly2(c.GetProperty("pts"), U.D(c, "elev"), true);
                                add.Invoke(coll, new[] { Activator.CreateInstance(contT, pl) });
                            }
                            var create = pondT.GetMethod("Create", new[] { typeof(Database), typeof(string), collT });
                            var id = (ObjectId)create.Invoke(null, new object[] { db, name, coll });
                            var pond = (AcEntity)tr.GetObject(id, OpenMode.ForWrite);
                            pond.Layer = layer;
                            var st = cat.Id("pond_style", O); if (!st.IsNull) U.TrySet(pond, "StyleId", st);
                            U.TrySet(pond, "Description", $"Detention {U.D(p, "storage_cf"):N0} CF, {U.D(p, "side_slope", 3):0}:1, top {U.D(p, "top"):0.00} bottom {U.D(p, "bottom"):0.00}");
                            // the storm pipe that discharges into the pond
                            foreach (var n in U.A(M, "networks"))
                                foreach (var s in U.A(n, "structures"))
                                    if (U.S(s, "pond") == name)
                                        foreach (var pp in U.A(n, "pipes"))
                                            if (U.S(pp, "to") == U.S(s, "name") && pipeByName.TryGetValue(U.S(pp, "name"), out var pid))
                                                U.TryCall(pond, "ConnectToPipe", pid, ConnectorPositionType.End);
                            tr.Commit();
                        }
                        report.Add($"Pond {name}: {cons.Count} contours, {U.D(p, "top"):0.00} to {U.D(p, "bottom"):0.00}");
                        continue;
                    }
                    catch (System.Exception e) { warn.Add($"Pond {name} could not be created as a pond object ({(e.InnerException ?? e).Message}); drawn as feature lines"); }
                }
                using (var tr = db.TransactionManager.StartTransaction())
                {
                    var site = SiteId(tr);
                    foreach (var c in new[] { cons.First(), cons.Last() })
                        FeatureFrom3d(tr, Poly3(c.GetProperty("pts"), U.D(c, "elev")), $"{name} {U.D(c, "elev"):0.00}", site, layer, cat);
                    tr.Commit();
                }
            }
        }

        // ------------------------------------------------------------------ grading feature lines
        ObjectId SiteId(Transaction tr)
        {
            foreach (ObjectId id in civ.GetSiteIds())
                if (((Site)tr.GetObject(id, OpenMode.ForRead)).Name == O.Site) return id;
            return Site.Create(civ, O.Site);
        }

        ObjectId FeatureFrom3d(Transaction tr, Polyline3d p3, string name, ObjectId site, string layer, Catalog cat)
        {
            var ms = ModelSpace(tr);
            var pid = ms.AppendEntity(p3);
            tr.AddNewlyCreatedDBObject(p3, true);
            var fid = FeatureLine.Create(name, pid, site);
            var fl = (FeatureLine)tr.GetObject(fid, OpenMode.ForWrite);
            fl.Layer = layer;
            var st = cat.Id("feature_style", O); if (!st.IsNull) fl.StyleId = st;
            p3.Erase();
            return fid;
        }

        void Features(Catalog cat)
        {
            int n = 0, nc = 0;
            var brk = new ObjectIdCollection();
            var layer = Layer("C-GRAD-FEAT");
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var site = SiteId(tr);
                foreach (var f in U.A(M, "feature_lines"))
                {
                    var pts = U.A(f, "pts").Select(U.XY).ToList();
                    if (pts.Count < 2) continue;
                    var pc = new Point3dCollection();
                    foreach (var q in pts) pc.Add(new Point3d(q[0], q[1], q[2]));
                    bool closed = U.B(f, "closed");
                    if (closed && pc.Count > 2 && pc[0].DistanceTo(pc[pc.Count - 1]) < 0.01) pc.RemoveAt(pc.Count - 1);
                    try
                    {
                        var fid = FeatureFrom3d(tr, new Polyline3d(Poly3dType.SimplePoly, pc, closed), U.S(f, "name"), site, layer, cat); n++;
                        var kind = U.S(f, "kind");
                        if (kind == "curb_bc" || kind == "curb_tc" || kind == "ada" || kind == "building") brk.Add(fid);
                        if (kind == "curb_bc" || kind == "curb_tc") nc++;
                    }
                    catch (System.Exception e) { warn.Add($"Feature line {U.S(f, "name")}: {e.Message}"); }
                }
                tr.Commit();
            }
            // curbs (TC / BC), ADA planes and building pads are breaklines of the FG surface, so the TIN carries the 6" curb
            if (!fgId.IsNull && brk.Count > 0)
                try
                {
                    using (var tr = db.TransactionManager.StartTransaction())
                    {
                        var t = (TinSurface)tr.GetObject(fgId, OpenMode.ForWrite);
                        t.BreaklinesDefinition.AddStandardBreaklines(brk, 0.1, 0.0, 0.0, 0.0);
                        t.Rebuild();
                        tr.Commit();
                    }
                }
                catch (System.Exception e) { warn.Add($"FG surface: the curb breaklines could not be added ({e.Message})"); }
            report.Add($"Feature lines: {n} in site \"{O.Site}\" ({nc} granite curb TC / BC lines{(brk.Count > 0 && !fgId.IsNull ? ", added to FG as breaklines" : "")})");
        }

        // ------------------------------------------------------------------ drive alignment + EG and design profiles
        void Alignments(Catalog cat)
        {
            foreach (var a in U.A(M, "alignments"))
            {
                using (var tr = db.TransactionManager.StartTransaction())
                {
                    var pl = Poly2(a.GetProperty("pts"), 0, false);
                    var ms = ModelSpace(tr);
                    var plId = ms.AppendEntity(pl);
                    tr.AddNewlyCreatedDBObject(pl, true);
                    var opt = new PolylineOptions { PlineId = plId, AddCurvesBetweenTangents = false, EraseExistingEntities = true };
                    var lay = LayerId(U.S(a, "layer", "C-ROAD-CNTR"));
                    var aStyle = cat.Id("align_style", O); var aLab = cat.Id("align_labels", O);
                    var name = U.S(a, "name", "Drive CL");
                    var alId = Alignment.Create(civ, opt, name, ObjectId.Null, lay, aStyle, aLab);
                    var player = LayerId("C-ROAD-PROF");
                    var pLab = cat.Id("profile_labels", O);
                    if (!egId.IsNull && a.TryGetProperty("eg_profile", out var egn) && egn.ValueKind == JsonValueKind.String)
                        Profile.CreateFromSurface(egn.GetString(), alId, egId, player, cat.Id("profile_eg_style", O), pLab);
                    if (a.TryGetProperty("profile", out var pr))
                    {
                        var pid = Profile.CreateByLayout(U.S(pr, "name", name + " - FG"), alId, player, cat.Id("profile_style", O), pLab);
                        var prof = (Profile)tr.GetObject(pid, OpenMode.ForWrite);
                        var curves = new List<(double, double, double)>();
                        foreach (var v in U.A(pr, "pvis"))
                        {
                            var q = U.XY(v);
                            prof.PVIs.AddPVI(q[0], q[1]);
                            if (q.Length > 2 && q[2] > 0) curves.Add((q[0], q[1], q[2]));
                        }
                        foreach (var (s, z, L) in curves)
                        {
                            try
                            {
                                var pvi = prof.PVIs.GetPVIAt(s, z);
                                U.TryCall(prof.Entities, "AddFreeSymmetricParabolaByPVIAndLength", pvi, L);
                            }
                            catch { }
                        }
                    }
                    tr.Commit();
                    report.Add($"Alignment {name} with EG and design profiles (create a profile view with PROFILEVIEW... to show them)");
                }
            }
        }

        // ------------------------------------------------------------------ COGO points
        void Points(Catalog cat)
        {
            int n = 0;
            var layer = Layer("C-TOPO-SPOT");
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var ps = cat.Id("point_style", O); var pl = cat.Id("point_label", O);
                foreach (var p in U.A(M, "points"))
                {
                    var xy = U.XY(p.GetProperty("xy"));
                    var id = civ.CogoPoints.Add(new Point3d(xy[0], xy[1], U.D(p, "z")), U.S(p, "desc"), true);
                    var cp = (CogoPoint)tr.GetObject(id, OpenMode.ForWrite);
                    cp.Layer = layer;
                    if (!ps.IsNull) cp.StyleId = ps;
                    if (!pl.IsNull) cp.LabelStyleId = pl;
                    n++;
                }
                tr.Commit();
            }
            report.Add($"COGO points: {n}");
        }

        // ------------------------------------------------------------------ linework (not Civil 3D objects) onto the firm's layers
        void Linework()
        {
            var path = Path.Combine(dir, U.S(M, "linework", "linework.dxf"));
            if (!File.Exists(path)) { warn.Add("linework.dxf not found next to manifest.json"); return; }
            var before = new HashSet<ObjectId>();
            using (var tr = db.TransactionManager.StartOpenCloseTransaction())
                foreach (ObjectId id in ModelSpace(tr, false)) before.Add(id);
            using (var side = new Database(false, true))
            {
                side.DxfIn(path, null);
                db.Insert(Matrix3d.Identity, side, false);
            }
            int n = 0;
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var cache = new Dictionary<string, string>();
                foreach (ObjectId id in ModelSpace(tr, false))
                {
                    if (before.Contains(id)) continue;
                    var e = (AcEntity)tr.GetObject(id, OpenMode.ForWrite);
                    if (O.Layers.TryGetValue(e.Layer, out var to) && !string.IsNullOrEmpty(to) && to != e.Layer)
                    {
                        if (!cache.TryGetValue(to, out var ok)) cache[to] = ok = Layer(to);
                        e.Layer = ok;
                    }
                    n++;
                }
                tr.Commit();
            }
            report.Add($"Linework: {n} objects (property, buildings, pavement, striping, water, force main, electric, gas, erosion control)");
        }

        // ------------------------------------------------------------------ geometry helpers
        static BlockTableRecord ModelSpace(Transaction tr, bool write = true)
        {
            var db = HostApplicationServices.WorkingDatabase;
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            return (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], write ? OpenMode.ForWrite : OpenMode.ForRead);
        }

        static Polyline Poly2(JsonElement pts, double elev, bool closed)
        {
            var pl = new Polyline();
            var list = pts.EnumerateArray().Select(U.XY).ToList();
            if (closed && list.Count > 2 && Math.Abs(list[0][0] - list[^1][0]) < 1e-6 && Math.Abs(list[0][1] - list[^1][1]) < 1e-6) list.RemoveAt(list.Count - 1);
            for (int i = 0; i < list.Count; i++) pl.AddVertexAt(i, new Point2d(list[i][0], list[i][1]), 0, 0, 0);
            pl.Closed = closed;
            pl.Elevation = elev;
            return pl;
        }

        static Polyline3d Poly3(JsonElement pts, double z)
        {
            var pc = new Point3dCollection();
            var list = pts.EnumerateArray().Select(U.XY).ToList();
            if (list.Count > 2 && Math.Abs(list[0][0] - list[^1][0]) < 1e-6 && Math.Abs(list[0][1] - list[^1][1]) < 1e-6) list.RemoveAt(list.Count - 1);
            foreach (var q in list) pc.Add(new Point3d(q[0], q[1], z));
            return new Polyline3d(Poly3dType.SimplePoly, pc, true);
        }
    }
}
