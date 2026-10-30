"""Exercise the forecast batch method with a fake service; load no ML artifacts."""

from pathlib import Path
import ast
import unittest


class ForecastBatchTests(unittest.TestCase):
    def test_one_forecast_failure_does_not_remove_other_items_weather(self):
        # Compile only this service method so the check needs neither PyTorch
        # nor a trained model. Its predict_batch dependency is a recording stub.
        source = Path(__file__).resolve().parents[1] / "src" / "eta_service.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "BicycleETAService"
        )
        method = next(
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == "predict_with_kma_batch"
        )
        method.returns = None
        for arg in method.args.args:
            arg.annotation = None
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        namespace = {"canonical_id": str}
        exec(compile(module, str(source), "exec"), namespace)

        class Station:
            def __init__(self, n):
                self.latitude = float(n)
                self.longitude = 127.0

        class Locations:
            def __getitem__(self, sid):
                return Station(sid)

        class Stations:
            loc = Locations()

        class Weather:
            def for_route(self, lat1, lon1, lat2, lon2, departure):
                if lat2 == 3:
                    raise TimeoutError()
                return {
                    "provider": "fake",
                    "unique_grid_calls": 1,
                    **{
                        k: {"grid_nx": 60, "grid_ny": 127}
                        for k in ("origin", "destination", "seoul")
                    },
                }

            def model_weather(self, forecast):
                return {"mean_temperature_c": 20}

        class Service:
            stations = Stations()
            weather_provider = Weather()

            def predict_batch(self, requests):
                self.requests = requests
                return [{"model": {}} for r in requests]

        fake = Service()
        requests = [
            {
                "start_station_id": "1",
                "end_station_id": str(i),
                "departure": "2026-10-16T08:00:00+09:00",
            }
            for i in (2, 3)
        ]
        results = namespace["predict_with_kma_batch"](fake, requests)
        self.assertEqual(fake.requests[0]["weather"], {"mean_temperature_c": 20})
        self.assertIsNone(fake.requests[1]["weather"])
        self.assertIsNone(results[0]["weather_error"])
        self.assertEqual(results[0]["model"]["weather_source"], "fake")
        self.assertEqual(results[1]["weather_error"], "TimeoutError")
        self.assertNotIn("weather_forecast", results[1])


if __name__ == "__main__":
    unittest.main()
