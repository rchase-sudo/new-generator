using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.ApplicationServices;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

[assembly: CommandClass(typeof(Drafted.C3D.Commands))]

namespace Drafted.C3D
{
    public class Commands
    {
        /// <summary>DRAFTEDIMPORT: build native Civil 3D objects from a Drafted export package (drafted_c3d.zip or its manifest.json)
        /// in the open drawing, with the drawing's (the firm template's) styles, parts lists and layers.</summary>
        [CommandMethod("DRAFTEDIMPORT", CommandFlags.Modal)]
        public void Import()
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            var ed = doc.Editor;
            var po = new PromptOpenFileOptions("\nDrafted Civil 3D package") { Filter = "Drafted package (*.zip;*.json)|*.zip;*.json" };
            var pr = ed.GetFileNameForOpen(po);
            if (pr.Status != PromptStatus.OK) return;
            string dir, manifestPath;
            try { (dir, manifestPath) = Unpack(pr.StringResult); }
            catch (System.Exception e) { ed.WriteMessage($"\nDrafted: could not read the package: {e.Message}"); return; }
            JsonDocument jd;
            try { jd = JsonDocument.Parse(File.ReadAllText(manifestPath)); }
            catch (System.Exception e) { ed.WriteMessage($"\nDrafted: manifest.json is not valid: {e.Message}"); return; }
            using (jd)
            {
                var M = jd.RootElement;
                if (U.S(M, "format") != "drafted-c3d") { ed.WriteMessage("\nDrafted: that file is not a Drafted Civil 3D package."); return; }
                Catalog cat;
                using (doc.LockDocument())
                using (var tr = doc.Database.TransactionManager.StartTransaction())
                {
                    cat = new Catalog(CivilApplication.ActiveDocument, doc.Database, tr);
                    tr.Commit();
                }
                var opt = new Options();
                using (var dlg = new ImportDialog(cat, opt, M))
                    if (AcApp.ShowModalDialog(dlg) != System.Windows.Forms.DialogResult.OK) return;
                var epsg = M.TryGetProperty("crs", out var crs) && crs.TryGetProperty("epsg", out var ep) && ep.ValueKind == JsonValueKind.Number ? ep.GetInt32() : 0;
                if (epsg > 0) ed.WriteMessage($"\nDrafted: coordinates are EPSG:{epsg} (state plane feet). Make sure this drawing's coordinate system matches.");
                var lines = new Importer(doc, M, dir, opt).Run(cat);
                ed.WriteMessage("\n\nDrafted import finished:\n  " + string.Join("\n  ", lines) + "\n");
                try { ed.Command("_.ZOOM", "_E"); } catch { }
            }
        }

        /// <summary>DRAFTEDSTANDARDS: write the open drawing's (template's) layers, styles and parts lists to a JSON file that can be
        /// uploaded to Drafted, so Drafted's exports use the firm's names before they ever reach Civil 3D.</summary>
        [CommandMethod("DRAFTEDSTANDARDS", CommandFlags.Modal)]
        public void Standards()
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            var ed = doc.Editor;
            var so = new PromptSaveFileOptions("\nSave firm standards for Drafted") { Filter = "Drafted standards (*.json)|*.json" };
            so.InitialFileName = Path.GetFileNameWithoutExtension(doc.Name) + "_drafted_standards.json";
            var pr = ed.GetFileNameForSave(so);
            if (pr.Status != PromptStatus.OK) return;
            var outp = new Dictionary<string, object> { ["format"] = "drafted-standards", ["version"] = 1, ["drawing"] = Path.GetFileName(doc.Name) };
            using (doc.LockDocument())
            using (var tr = doc.Database.TransactionManager.StartTransaction())
            {
                var cat = new Catalog(CivilApplication.ActiveDocument, doc.Database, tr);
                var layers = new List<Dictionary<string, object>>();
                var lt = (LayerTable)tr.GetObject(doc.Database.LayerTableId, OpenMode.ForRead);
                foreach (ObjectId id in lt)
                {
                    var r = (LayerTableRecord)tr.GetObject(id, OpenMode.ForRead);
                    if (r.Name.Contains("|")) continue;
                    string ltn = "";
                    try { ltn = ((LinetypeTableRecord)tr.GetObject(r.LinetypeObjectId, OpenMode.ForRead)).Name; } catch { }
                    layers.Add(new Dictionary<string, object> { ["name"] = r.Name, ["color"] = (int)r.Color.ColorIndex, ["linetype"] = ltn });
                }
                outp["layers"] = layers;
                outp["styles"] = Catalog.Kinds.GroupBy(k => k.Path).ToDictionary(g => g.Key, g => cat.Sets[g.Key].Keys.OrderBy(x => x).ToList());
                tr.Commit();
            }
            File.WriteAllText(pr.StringResult, JsonSerializer.Serialize(outp, new JsonSerializerOptions { WriteIndented = true }));
            ed.WriteMessage($"\nDrafted: firm standards written to {pr.StringResult}. Upload it in Drafted under Civil 3D template.");
        }

        static (string, string) Unpack(string path)
        {
            if (path.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return (Path.GetDirectoryName(path), path);
            var dir = Path.Combine(Path.GetTempPath(), "Drafted", Path.GetFileNameWithoutExtension(path) + "_" + DateTime.Now.ToString("yyyyMMddHHmmss"));
            Directory.CreateDirectory(dir);
            ZipFile.ExtractToDirectory(path, dir, true);
            var m = Directory.GetFiles(dir, "manifest.json", SearchOption.AllDirectories).FirstOrDefault()
                    ?? throw new FileNotFoundException("manifest.json is not in the zip");
            return (Path.GetDirectoryName(m), m);
        }
    }
}
