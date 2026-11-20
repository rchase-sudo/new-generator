using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Drafted.C3D
{
    /// <summary>Small helpers: JSON access, reflection for API members that differ between Civil 3D releases, layer auto-mapping.</summary>
    internal static class U
    {
        public static double D(JsonElement e, string k, double dflt = 0)
        {
            if (e.ValueKind == JsonValueKind.Object && e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number) return v.GetDouble();
            return dflt;
        }

        public static string S(JsonElement e, string k, string dflt = "")
        {
            if (e.ValueKind == JsonValueKind.Object && e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String) return v.GetString();
            return dflt;
        }

        public static bool B(JsonElement e, string k)
        {
            return e.ValueKind == JsonValueKind.Object && e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.True;
        }

        public static IEnumerable<JsonElement> A(JsonElement e, string k)
        {
            if (e.ValueKind == JsonValueKind.Object && e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Array)
                foreach (var x in v.EnumerateArray()) yield return x;
        }

        public static double[] XY(JsonElement e)
        {
            var a = e.EnumerateArray().Select(x => x.GetDouble()).ToArray();
            return a;
        }

        /// <summary>Set a property if this release of the API has it (and it is writable). Returns false otherwise.</summary>
        public static bool TrySet(object o, string prop, object value)
        {
            try
            {
                var p = o.GetType().GetProperty(prop, BindingFlags.Public | BindingFlags.Instance);
                if (p == null || !p.CanWrite) return false;
                var t = Nullable.GetUnderlyingType(p.PropertyType) ?? p.PropertyType;
                p.SetValue(o, t.IsInstanceOfType(value) ? value : Convert.ChangeType(value, t));
                return true;
            }
            catch { return false; }
        }

        public static object TryGet(object o, string prop)
        {
            try { return o.GetType().GetProperty(prop, BindingFlags.Public | BindingFlags.Instance)?.GetValue(o); }
            catch { return null; }
        }

        /// <summary>Call a public instance method by name with these arguments, if it exists. Returns (ok, result).</summary>
        public static (bool, object) TryCall(object o, string method, params object[] args)
        {
            try
            {
                foreach (var m in o.GetType().GetMethods(BindingFlags.Public | BindingFlags.Instance).Where(m => m.Name == method))
                {
                    var ps = m.GetParameters();
                    if (ps.Length != args.Length) continue;
                    bool fit = true;
                    for (int i = 0; i < ps.Length; i++)
                        if (args[i] != null && !ps[i].ParameterType.IsInstanceOfType(args[i]) && !(ps[i].ParameterType.IsPrimitive && args[i] is IConvertible)) { fit = false; break; }
                    if (!fit) continue;
                    var conv = args.Select((a, i) => a != null && ps[i].ParameterType.IsPrimitive && !ps[i].ParameterType.IsInstanceOfType(a) ? Convert.ChangeType(a, ps[i].ParameterType) : a).ToArray();
                    return (true, m.Invoke(o, conv));
                }
            }
            catch (TargetInvocationException) { }
            catch (Exception) { }
            return (false, null);
        }

        static string[] Toks(string s) => Regex.Split(s.ToUpperInvariant(), "[^A-Z0-9]+").Where(t => t.Length > 0).ToArray();

        static readonly HashSet<string> Generic = new HashSet<string> { "PIPE", "TEXT", "LABL", "ANNO", "STRC", "STRUCT", "EDGE", "LAT", "LATL", "SERV", "SVC", "CONC", "ELEV", "POINT", "MH", "SD", "SS", "SF", "CL", "PL", "FM", "UE", "FH" };

        static readonly string[] ExHint = { "EX", "EXIST", "EXST", "V-", "EXT" };

        /// <summary>The firm layer most likely to hold a Drafted standard layer's content (same rules as Drafted's server side).</summary>
        public static string AutoLayer(string std, string[] words, bool existing, IList<string> layers)
        {
            var exact = layers.FirstOrDefault(l => string.Equals(l, std, StringComparison.OrdinalIgnoreCase));
            if (exact != null) return exact;
            var sw = new HashSet<string>(Toks(std));
            string best = null; double bs = 0;
            foreach (var n in layers)
            {
                var ts = new HashSet<string>(Toks(n));
                double hit = words.Sum(w => ts.Contains(w) ? 2.0 : 0.0) + words.Sum(w => w.Length > 2 && ts.Any(x => x != w && x.Contains(w)) ? 0.5 : 0.0);
                if (!words.Where(w => !Generic.Contains(w)).Any(w => ts.Contains(w) || (w.Length > 2 && ts.Any(x => x.Contains(w))))) continue;
                double s = hit + 0.5 * ts.Count(sw.Contains);
                bool ex = ExHint.Any(h => n.ToUpperInvariant().StartsWith(h)) || n.ToUpperInvariant().Contains("EXIST");
                if (existing) s += ex ? 1.5 : -1.0; else if (ex) s -= 2.5;
                if (s > bs) { bs = s; best = n; }
            }
            return bs >= 2.0 ? best : null;
        }

        // Drafted's standard layers: description, the words a firm layer for it tends to contain, existing?
        public static readonly (string Key, string Desc, string[] Words, bool Existing)[] Roles =
        {
            ("V-TOPO", "Existing surface (EG)", new[] { "SURF", "TOPO", "EG", "EXIST", "TIN" }, true),
            ("C-TOPO", "Proposed surface (FG)", new[] { "SURF", "TOPO", "FG", "FINISH", "GRAD", "TIN" }, false),
            ("C-STRM-PIPE", "Storm pipes", new[] { "STRM", "STORM", "SD", "DRAN", "DRAIN", "PIPE" }, false),
            ("C-STRM-STRC", "Storm structures", new[] { "STRM", "STORM", "SD", "DRAN", "STRC", "STRUCT", "CB", "MH" }, false),
            ("C-SSWR-PIPE", "Sanitary pipes", new[] { "SSWR", "SAN", "SANI", "SEWR", "SEWER", "SS", "PIPE" }, false),
            ("C-SSWR-STRC", "Sanitary manholes", new[] { "SSWR", "SAN", "SEWR", "STRC", "MH", "SMH" }, false),
            ("C-STRM-POND", "Detention ponds", new[] { "POND", "BASN", "BASIN", "DETN", "SWM", "STRM" }, false),
            ("C-GRAD-FEAT", "Grading feature lines", new[] { "FEAT", "FTLN", "GRAD", "BRKL", "BREAK" }, false),
            ("C-ROAD-CNTR", "Drive alignment", new[] { "CNTR", "CL", "CENTER", "ALIGN", "ALGN" }, false),
            ("C-ROAD-PROF", "Drive profiles", new[] { "PROF", "PROFILE", "PRFL" }, false),
            ("C-TOPO-SPOT", "COGO points", new[] { "SPOT", "ELEV", "POINT", "PNTS", "COGO" }, false),
            ("V-PROP-LINE", "Property line", new[] { "PROP", "BNDY", "BOUNDARY", "PL", "LOT", "PARCEL" }, true),
            ("V-PROP-SBCK", "Setback lines", new[] { "SBCK", "SETBACK", "SETB", "BSL", "ESMT" }, true),
            ("V-ROAD-EDGE", "Existing road edge", new[] { "ROAD", "EOP", "PVMT", "EDGE", "CURB" }, true),
            ("V-BLDG", "Existing buildings", new[] { "BLDG", "BUILDING", "STRC" }, true),
            ("C-BLDG", "Proposed buildings", new[] { "BLDG", "BUILDING", "STRC", "FTPT" }, false),
            ("C-PVMT-EDGE", "Pavement edge / curb", new[] { "PVMT", "PAVE", "CURB", "EOP", "EDGE", "ASPH" }, false),
            ("C-PKNG-STRP", "Parking striping", new[] { "STRP", "STRIPE", "PKNG", "PARK", "PAINT", "MRKG" }, false),
            ("C-WALK", "Sidewalks", new[] { "WALK", "SDWK", "SIDEWALK", "CONC" }, false),
            ("C-SSWR-FMAN", "Force main (polyline)", new[] { "FMAN", "FM", "FORCE", "SSWR", "SAN" }, false),
            ("C-SSWR-LATL", "Sanitary laterals", new[] { "LATL", "LAT", "SERV", "SVC", "SSWR", "SAN" }, false),
            ("C-EROS-MATT", "2:1 slopes - erosion control matting", new[] { "MATT", "MAT", "BLANKET", "ECB", "EROS" }, false),
            ("C-SITE-RIPR", "1:1 slopes - riprap", new[] { "RIPR", "RIP", "RIPRAP", "STONE" }, false),
            ("C-SITE-WALL", "Retaining walls", new[] { "WALL", "RETN", "RTWL" }, false),
            ("C-WATR-PIPE", "Water main (polyline)", new[] { "WATR", "WATER", "WTR", "WM", "DOM" }, false),
            ("C-WATR-LATL", "Water services", new[] { "WATR", "WATER", "WTR", "LATL", "SERV", "SVC" }, false),
            ("C-WATR-STRC", "Hydrants and valves", new[] { "WATR", "WATER", "HYDR", "HYD", "VALV", "VALVE", "FH" }, false),
            ("C-POWR-UGND", "Underground electric", new[] { "POWR", "ELEC", "ELECTRIC", "UGND", "UE" }, false),
            ("C-NGAS-UGND", "Gas", new[] { "NGAS", "GAS" }, false),
            ("C-EROS-LOD", "Limit of disturbance", new[] { "LOD", "LIMIT", "DIST", "EROS", "ESC" }, false),
            ("C-EROS-SILT", "Silt fence", new[] { "SILT", "SF", "EROS", "ESC", "FENC" }, false),
        };

        /// <summary>Best match among names by keywords (case-insensitive, first keyword wins), else fallback.</summary>
        public static string Pick(IList<string> names, string preferred, params string[] keys)
        {
            if (!string.IsNullOrEmpty(preferred))
            {
                var p = names.FirstOrDefault(n => string.Equals(n, preferred, StringComparison.OrdinalIgnoreCase));
                if (p != null) return p;
            }
            foreach (var k in keys)
            {
                var hit = names.FirstOrDefault(n => n.IndexOf(k, StringComparison.OrdinalIgnoreCase) >= 0);
                if (hit != null) return hit;
            }
            return names.FirstOrDefault(n => n.Equals("Standard", StringComparison.OrdinalIgnoreCase)) ?? names.FirstOrDefault();
        }
    }
}
