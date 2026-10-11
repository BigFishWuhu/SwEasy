from pathlib import Path

import shutil

assets_dir = Path(__file__).parent.parent.resolve() / "assets"


def configure_ocr_model():
    ocr_dir = assets_dir / "resource" / "model" / "ocr"
    if all((ocr_dir / name).is_file() for name in ("det.onnx", "rec.onnx", "keys.txt")):
        print("Found existing OCR model, skipping default OCR model import.")
        return
    assets_ocr_dir = assets_dir / "MaaCommonAssets" / "OCR"
    if not assets_ocr_dir.exists():
        print(f"File Not Found: {assets_ocr_dir}")
        exit(1)

    shutil.copytree(
        assets_ocr_dir / "ppocr_v6" / "small",
        ocr_dir,
        dirs_exist_ok=True,
    )


if __name__ == "__main__":
    configure_ocr_model()

    print("OCR model configured.")
