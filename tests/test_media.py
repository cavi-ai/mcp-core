import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from cavi_mcp_core import media


class PngContentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "frame.png"
        Image.new("RGBA", (2000, 1000), (240, 10, 0, 123)).save(self.path)

    def decoded(self, content):
        self.assertEqual((content.type, content.mime_type), ("image", "image/png"))
        return Image.open(io.BytesIO(base64.b64decode(content.data)))

    def test_an_edge_scales_the_long_side_down_to_it(self):
        image = self.decoded(media.png_content(self.path, 768))
        self.assertEqual((image.format, image.size), ("PNG", (768, 384)))

    def test_without_an_edge_the_image_keeps_its_size_and_pixels(self):
        image = self.decoded(media.png_content(self.path))
        self.assertEqual((image.size, image.getpixel((5, 5))), ((2000, 1000), (240, 10, 0, 123)))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        for index, color in enumerate(((240, 10, 0, 123), (0, 220, 90, 0))):
            Image.new("RGBA", (4, 3), color).save(self.root / f"part{index}.png")
        self.spec = {"version": 1, "name": "Walk",
                     "fps": {"numerator": 48000, "denominator": 2002},
                     "alpha": "straight", "loop": True, "sources": [],
                     "frames": [{"name": f"Pose{i}", "path": f"part{i}.png"} for i in (1, 0)]}
        media.write(self.root / "recipe.json", self.spec)

    def test_ingest_pins_ordered_frames_and_sources(self):
        result = media.ingest(self.root, "recipe.json", "ingest")
        self.assertEqual(result["receipt"], str(self.root / "ingest/media.json"))
        self.assertEqual(result["frame_count"], 2)
        self.assertEqual(result["fps"], {"numerator": 24000, "denominator": 1001})
        data = json.loads((self.root / "ingest/media.json").read_text())
        self.assertEqual(data["schema"], "cavi.animation-media.v1")
        self.assertEqual([frame["path"] for frame in data["frames"]], ["part1.png", "part0.png"])
        self.assertEqual(data["frames"][0]["sha256"], media.digest(self.root / "part1.png"))
        self.assertEqual(data["frames"][0]["rect"], [0, 0, 4, 3])
        self.assertEqual(data["sources"], [
            {"path": "recipe.json", "sha256": media.digest(self.root / "recipe.json")}])
        self.assertEqual(media.validate(self.root, "ingest/media.json"), result)

    def test_refuses_bad_recipes_before_creating_the_directory(self):
        for key, value in (("fps", {"numerator": True, "denominator": 1}),
                           ("alpha", "premultiplied"), ("frames", [self.spec["frames"][0]] * 2),
                           ("frames", [{"name": "Out", "path": "../part0.png"}])):
            with self.subTest(key=key, value=value):
                (self.root / "recipe.json").write_text(json.dumps({**self.spec, key: value}))
                with self.assertRaises(ValueError):
                    media.ingest(self.root, "recipe.json", "unused")
                self.assertFalse((self.root / "unused").exists())

    def test_refuses_an_existing_directory_and_changed_frames(self):
        result = media.ingest(self.root, "recipe.json", "ingest")
        with self.assertRaises(FileExistsError):
            media.ingest(self.root, "recipe.json", "ingest")
        Image.new("RGBA", (4, 3), "blue").save(self.root / "part0.png")
        with self.assertRaisesRegex(ValueError, "Frame input changed"):
            media.validate(self.root, result["receipt"])

    def test_refuses_a_changed_source(self):
        result = media.ingest(self.root, "recipe.json", "ingest")
        (self.root / "recipe.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Source changed: recipe.json"):
            media.validate(self.root, result["receipt"])


@unittest.skipIf(sys.platform == "win32", "run is POSIX only")
class RunTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)

    def test_records_a_finished_command(self):
        state = media.run([sys.executable, "-c", "print('done')"], self.directory)
        self.assertEqual(state["state"], "exited")
        self.assertEqual(state["exit_code"], 0)
        self.assertEqual((self.directory / "native.log").read_text(), "done\n")
        self.assertEqual(json.loads((self.directory / "execution.json").read_text()), state)

    def test_a_failed_command_is_recorded_as_unknown(self):
        with self.assertRaises(media.MediaOutcomeUnknown):
            media.run([sys.executable, "-c", "raise SystemExit(3)"], self.directory)
        execution = json.loads((self.directory / "execution.json").read_text())
        self.assertEqual(execution["state"], "unknown")
        self.assertEqual(execution["exit_code"], 3)
        self.assertFalse(execution["retry_safe"])

    def test_a_command_past_its_deadline_is_stopped(self):
        with self.assertRaises(media.MediaOutcomeUnknown):
            media.run([sys.executable, "-c", "import time; time.sleep(30)"], self.directory, 0.3)
        execution = json.loads((self.directory / "execution.json").read_text())
        self.assertEqual(execution["stopped_by_guard"], "time_or_log_limit")
        self.assertLess(execution["elapsed_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
