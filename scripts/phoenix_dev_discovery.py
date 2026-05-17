#!/usr/bin/env python3
"""Phoenix Development Intelligence — ArcGIS Discovery Script

Investigates the Phoenix Planning_Permit MapServer and related services.
Saves metadata, sample queries, and prints a summary table.

Usage:
    .venv/bin/python scripts/phoenix_dev_discovery.py

Output:
    - data/phoenix/arcgis_layers.json   — full layer metadata
    - data/phoenix/samples/layer_{id}_{name}.json  — 5-record samples
    - stdout summary table
"""

import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = "https://maps.phoenix.gov/pub/rest/services/Public/Planning_Permit/MapServer"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "phoenix")
SAMPLE_DIR = os.path.join(OUT_DIR, "samples")

# Layers we care about for permit lifecycle
PROMISING_LAYERS = {0, 1, 2, 4, 5}

def fetch_json(url):
    """Fetch a JSON response from a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", file=sys.stderr)
        return None


def main():
    os.makedirs(SAMPLE_DIR, exist_ok=True)

    # 1. Get MapServer root metadata (layer list)
    print("=== Planning_Permit MapServer Discovery ===\n")
    root = fetch_json(f"{BASE_URL}?f=pjson")
    if not root:
        print("FATAL: Could not fetch MapServer root", file=sys.stderr)
        sys.exit(1)

    all_layers = root.get("layers", []) + [{"id": t["id"], "name": t["name"], "type": "Table"} for t in root.get("tables", [])]
    layers_meta = {}
    layer_details = []

    # 2. Get individual layer metadata
    for layer in all_layers:
        lid = layer["id"]
        lname = layer["name"]
        print(f"  Fetching layer {lid}: {lname}...")
        meta = fetch_json(f"{BASE_URL}/{lid}?f=pjson")
        if meta:
            layers_meta[lid] = meta
            # Extract key fields
            fields = meta.get("fields", [])
            date_fields = [f["name"] for f in fields if f["type"] == "esriFieldTypeDate"]
            string_fields = [f["name"] for f in fields if f["type"] == "esriFieldTypeString"]

            # Get record count
            count_url = f"{BASE_URL}/{lid}/query?where=1%3D1&f=json&returnCountOnly=true"
            count_resp = fetch_json(count_url)
            count = count_resp.get("count", 0) if count_resp else -1

            # Relevance scoring
            relevance = "Low"
            lname_lower = lname.lower()
            lid_int = int(lid)
            if lid_int in PROMISING_LAYERS:
                relevance = "HIGH"
            elif any(kw in lname_lower for kw in ["zoning", "overlay", "general plan", "historic", "village"]):
                relevance = "MEDIUM"

            desc = meta.get("description") or ""
            gtype = meta.get("geometryType") or "N/A"
            mtype = meta.get("type", "Table")
            aqc = meta.get("advancedQueryCapabilities") or {}
            layer_details.append({
                "id": lid,
                "name": lname,
                "type": mtype,
                "geometry_type": gtype,
                "count": count,
                "date_fields": date_fields,
                "key_string_fields": string_fields[:8],
                "relevance": relevance,
                "description": desc[:120],
                "supports_pagination": aqc.get("supportsPagination", False),
                "max_record_count": meta.get("maxRecordCount", 1000)
            })
        else:
            print(f"  WARNING: Could not fetch layer {lid} metadata")

    # 3. Save full metadata
    meta_path = os.path.join(OUT_DIR, "arcgis_layers.json")
    with open(meta_path, "w") as f:
        json.dump(layers_meta, f, indent=2)
    print(f"\n  Saved full layer metadata to {meta_path}")

    # 4. Query samples from promising layers
    sample_results = {}
    for ld in layer_details:
        if int(ld["id"]) in PROMISING_LAYERS or ld["relevance"] == "HIGH":
            lid = ld["id"]
            lname = ld["name"]
            print(f"\n  Fetching 5 samples from layer {lid}: {lname}...")
            sample_url = f"{BASE_URL}/{lid}/query?where=1%3D1&outFields=*&f=json&resultRecordCount=5"
            sample = fetch_json(sample_url)
            if sample:
                # Strip geometry to reduce file size
                features = sample.get("features", [])
                clean_features = []
                for feat in features:
                    clean_features.append({"attributes": feat.get("attributes", {})})
                sample_data = {
                    "layer_id": lid,
                    "layer_name": lname,
                    "field_aliases": sample.get("fieldAliases", {}),
                    "features": clean_features
                }
                sample_results[lid] = sample_data
                sample_path = os.path.join(SAMPLE_DIR, f"layer_{lid}_{lname.replace(' ', '_')}.json")
                with open(sample_path, "w") as f:
                    json.dump(sample_data, f, indent=2)
                print(f"    Saved to {sample_path}")
            else:
                print(f"    WARNING: Could not fetch samples for layer {lid}")

    # 5. Print summary table
    print("\n" + "=" * 120)
    print(f"{'ID':<4} {'Layer Name':<28} {'Geom Type':<18} {'Count':<8} {'Date Fields':<30} {'Key Fields':<30} {'Relevance':<10}")
    print("=" * 120)
    for ld in sorted(layer_details, key=lambda x: (0 if x["relevance"] == "HIGH" else 1 if x["relevance"] == "MEDIUM" else 2, x["id"])):
        date_str = ", ".join(ld["date_fields"][:3]) if ld["date_fields"] else "N/A"
        key_str = ", ".join(ld["key_string_fields"][:4]) if ld["key_string_fields"] else "N/A"
        count_str = str(ld["count"]) if ld["count"] >= 0 else "?"
        print(f"{ld['id']:<4} {ld['name']:<28} {ld['geometry_type']:<18} {count_str:<8} {date_str:<30} {key_str:<30} {ld['relevance']:<10}")

    print("\n" + "=" * 120)
    print("Legend: HIGH = core permit lifecycle, MEDIUM = zoning/planning reference, Low = supplementary")
    print("\nSee data/phoenix/arcgis_layers.json for full metadata.")
    print("See data/phoenix/samples/ for 5-record sample queries.")


if __name__ == "__main__":
    main()
