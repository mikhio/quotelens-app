"""Cross-runtime agreement for the dated real catalog used by the UI review."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from quotelens.engine import analyze_offers, parse_request
from quotelens.retrieval import SOURCES
from quotelens.scenarios import load_scenario

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for the optional UI catalog regression suite")
class CatalogAdapterAgreement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample = load_scenario("real-oscilloscopes")
        cls.offers = copy.deepcopy(cls.sample["offers"])
        for offer in cls.offers:
            offer.pop("image", None)
        cls.cases = [
            {"description": "Осциллограф АКИП-4131/1В, 4 канала, полоса не менее 100 МГц", "quantity": 3},
            {"description": "Осциллограф цифровой AKIP-4131/1V, 4 канала, полоса не менее 100 МГц", "quantity": 3},
            {"description": "Осциллограф, 4 канала, частота дискретизации 1 ГГц, полоса не менее 100 МГц", "quantity": 3},
            {"description": "Осциллограф, 4 канала, полоса 100 МГц или 200 МГц", "quantity": 3},
            {"description": "Осциллограф, 4 канала, частота дискретизации 1 ГГц", "quantity": 3},
            {"description": "Осциллограф, 4 канала, 100 МГц", "quantity": 3},
            {"description": "Осциллограф, не менее 2 каналов, полоса не менее 30 МГц", "quantity": 3},
            {"description": "Осциллограф, 4 канала, полоса не менее 100 МГц", "quantity": 3, "source_ids": ["prist", "souz"]},
            {"description": "Осциллограф, 4 канала, полоса не менее 100 МГц", "quantity": .001},
            {"description": "Осциллограф, 4 канала, полоса не менее 100 МГц", "quantity": .005},
            {"description": "Осциллограф, 4 канала, полоса не менее 100 МГц", "quantity": 3.333},
            {"description": "Осциллограф, 4 канала, полоса 100 МГц, полоса 200 МГц", "quantity": 3},
            {"description": "Осциллограф ADS-2031, 2 канала, полоса 30 МГц", "quantity": 1},
        ]
        domains = {o["domain"] for o in cls.offers}
        catalog = {"offers": cls.offers, "sources": [s for s in SOURCES if s["domain"] in domains],
                   "config": {}, "captured_at": cls.sample.get("captured_at"), "date_label": "11.11.2026", "description": cls.cases[0]["description"], "quantity": 3}
        program = r'''
const fs=require('fs');const data=JSON.parse(fs.readFileSync(0,'utf8'));
global.window={QUOTE_LENS_CATALOG:data.catalog};
eval(fs.readFileSync(process.argv[1],'utf8'));
(async()=>{const results=[];for(const payload of data.cases){const job=await window.QuoteLens.request('/api/search',{method:'POST',body:JSON.stringify(payload)});results.push((await window.QuoteLens.request('/api/jobs/'+job.id)).result);}console.log(JSON.stringify(results));})().catch(e=>{console.error(String(e));process.exitCode=1;});
'''
        run = subprocess.run([shutil.which("node"), "-e", program, str(ROOT / "scripts/catalog_preview.js")],
                             input=json.dumps({"catalog": catalog, "cases": cls.cases}, ensure_ascii=False),
                             text=True, capture_output=True, timeout=10)
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.results = json.loads(run.stdout)

    def test_public_prices_decisions_and_required_fields_match_backend(self):
        for case, result in zip(self.cases, self.results):
            with self.subTest(query=case["description"], quantity=case["quantity"]):
                offers = copy.deepcopy(self.offers)
                if "source_ids" in case:
                    domains = {s["domain"] for s in SOURCES if s["id"] in case["source_ids"]}
                    offers = [o for o in offers if o["domain"] in domains]
                reference = analyze_offers(parse_request(case["description"], case["quantity"]), offers)
                for key in ["status", "n", "independent_suppliers", "unit_price", "total"]:
                    self.assertEqual(result["calculation"][key], reference["calculation"][key], key)
                self.assertEqual({o["id"]: o["status"] for o in result["offers"]}, {o["id"]: o["status"] for o in reference["offers"]})
                required = lambda r: [(x["key"], x["op"], x["value"]) for x in r["request"]["requirements"]]
                self.assertEqual(required(result), required(reference))

    def test_ambiguous_bandwidth_never_produces_a_ready_total(self):
        result = self.results[3]
        self.assertIsNone(result["calculation"]["total"])
        self.assertEqual(result["calculation"]["status"], "insufficient_data")

    def test_latin_model_suffix_agrees_with_real_cyrillic_model(self):
        self.assertEqual(self.results[1]["calculation"]["total"], 207195)
        self.assertEqual(self.results[1]["calculation"]["n"], 3)


if __name__ == "__main__":
    unittest.main()
