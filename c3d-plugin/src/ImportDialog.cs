using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Windows.Forms;

namespace Drafted.C3D
{
    /// <summary>Pick styles, parts lists and layers from the open drawing (the firm's template). Choices are remembered per user.</summary>
    internal class ImportDialog : Form
    {
        readonly Catalog cat;
        readonly Options opt;
        readonly Dictionary<string, ComboBox> combos = new Dictionary<string, ComboBox>();
        readonly DataGridView grid = new DataGridView();
        readonly Dictionary<string, CheckBox> checks = new Dictionary<string, CheckBox>();
        readonly TextBox site = new TextBox();
        static readonly string Store = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Drafted", "c3d_import.json");

        public ImportDialog(Catalog cat, Options opt, JsonElement manifest)
        {
            this.cat = cat; this.opt = opt;
            Text = "Drafted import - " + U.S(manifest.TryGetProperty("project", out var pj) ? pj : default, "name", "project");
            Width = 760; Height = 640; StartPosition = FormStartPosition.CenterScreen; MinimizeBox = false; MaximizeBox = false;
            Font = new Font("Segoe UI", 9f);
            var remembered = Load();
            var mStyles = manifest.TryGetProperty("styles", out var ms) && ms.ValueKind == JsonValueKind.Object ? ms : default;
            var mMap = manifest.TryGetProperty("layers", out var ml) && ml.TryGetProperty("mapping", out var mm) && mm.ValueKind == JsonValueKind.Object ? mm : default;

            var tabs = new TabControl { Dock = DockStyle.Fill };
            // --- objects + styles
            var t1 = new TabPage("Objects and styles");
            var tbl = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, AutoScroll = true, Padding = new Padding(10) };
            tbl.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 200));
            tbl.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            var what = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill };
            foreach (var (k, label) in new[] { ("surfaces", "Surfaces"), ("networks", "Pipe networks"), ("ponds", "Ponds"), ("features", "Feature lines"), ("alignments", "Alignment + profiles"), ("points", "COGO points"), ("linework", "Linework") })
            {
                var c = new CheckBox { Text = label, Checked = true, AutoSize = true };
                checks[k] = c; what.Controls.Add(c);
            }
            tbl.Controls.Add(new Label { Text = "Import", AutoSize = true, Anchor = AnchorStyles.Left }); tbl.Controls.Add(what);
            tbl.Controls.Add(new Label { Text = "Site for feature lines", AutoSize = true, Anchor = AnchorStyles.Left });
            site.Text = remembered.TryGetValue("_site", out var sv) ? sv : "Drafted"; site.Width = 250; tbl.Controls.Add(site);
            foreach (var k in Catalog.Kinds)
            {
                var names = cat.Names(k.Key);
                var cb = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 400 };
                cb.Items.Add("(drawing default)");
                foreach (var n in names) cb.Items.Add(n);
                string pref = remembered.TryGetValue(k.Key, out var r) ? r : (mStyles.ValueKind == JsonValueKind.Object ? U.S(mStyles, k.Key) : null);
                var pick = names.Count > 0 ? U.Pick(names, pref, k.Prefer) : null;
                cb.SelectedItem = pick ?? "(drawing default)";
                if (cb.SelectedIndex < 0) cb.SelectedIndex = 0;
                combos[k.Key] = cb;
                tbl.Controls.Add(new Label { Text = k.Label + (names.Count == 0 ? " (none in drawing)" : ""), AutoSize = true, Anchor = AnchorStyles.Left });
                tbl.Controls.Add(cb);
            }
            t1.Controls.Add(tbl);
            // --- layers
            var t2 = new TabPage("Layers");
            grid.Dock = DockStyle.Fill; grid.AllowUserToAddRows = false; grid.RowHeadersVisible = false; grid.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill;
            grid.Columns.Add(new DataGridViewTextBoxColumn { HeaderText = "Drafted layer", ReadOnly = true, FillWeight = 30 });
            grid.Columns.Add(new DataGridViewTextBoxColumn { HeaderText = "Holds", ReadOnly = true, FillWeight = 35 });
            var col = new DataGridViewComboBoxColumn { HeaderText = "Your layer", FillWeight = 45, FlatStyle = FlatStyle.Flat };
            var layerChoices = new List<string> { "(create Drafted layer)" };
            layerChoices.AddRange(cat.Layers);
            col.Items.AddRange(layerChoices.ToArray());
            grid.Columns.Add(col);
            foreach (var role in U.Roles)
            {
                string m = remembered.TryGetValue("L:" + role.Key, out var rl) ? rl : (mMap.ValueKind == JsonValueKind.Object ? U.S(mMap, role.Key) : null);
                if (m != null && !cat.Layers.Contains(m, StringComparer.OrdinalIgnoreCase)) m = null;
                m ??= U.AutoLayer(role.Key, role.Words, role.Existing, cat.Layers);
                var i = grid.Rows.Add(role.Key, role.Desc, m != null ? cat.Layers.First(x => string.Equals(x, m, StringComparison.OrdinalIgnoreCase)) : "(create Drafted layer)");
            }
            t2.Controls.Add(grid);
            tabs.TabPages.Add(t1); tabs.TabPages.Add(t2);

            var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, FlowDirection = FlowDirection.RightToLeft, Height = 44, Padding = new Padding(8) };
            var ok = new Button { Text = "Import", DialogResult = DialogResult.OK, Width = 100 };
            var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Width = 100 };
            bar.Controls.Add(ok); bar.Controls.Add(cancel);
            AcceptButton = ok; CancelButton = cancel;
            Controls.Add(tabs); Controls.Add(bar);
            FormClosing += (s, e) => { if (DialogResult == DialogResult.OK) Collect(); };
        }

        void Collect()
        {
            opt.Surfaces = checks["surfaces"].Checked; opt.Networks = checks["networks"].Checked; opt.Ponds = checks["ponds"].Checked;
            opt.Features = checks["features"].Checked; opt.Alignments = checks["alignments"].Checked; opt.Points = checks["points"].Checked; opt.Linework = checks["linework"].Checked;
            opt.Site = string.IsNullOrWhiteSpace(site.Text) ? "Drafted" : site.Text.Trim();
            var keep = new Dictionary<string, string> { ["_site"] = opt.Site };
            foreach (var kv in combos)
            {
                var v = kv.Value.SelectedItem as string;
                if (v != null && v != "(drawing default)") { opt.Styles[kv.Key] = v; keep[kv.Key] = v; }
            }
            foreach (DataGridViewRow r in grid.Rows)
            {
                var std = r.Cells[0].Value as string; var v = r.Cells[2].Value as string;
                if (std != null && v != null && v != "(create Drafted layer)") { opt.Layers[std] = v; keep["L:" + std] = v; }
            }
            try { Directory.CreateDirectory(Path.GetDirectoryName(Store)); File.WriteAllText(Store, JsonSerializer.Serialize(keep)); } catch { }
        }

        static Dictionary<string, string> Load()
        {
            try { return JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(Store)) ?? new Dictionary<string, string>(); }
            catch { return new Dictionary<string, string>(); }
        }
    }
}
