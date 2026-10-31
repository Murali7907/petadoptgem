import os
from pathlib import Path
from PIL import Image
from rest_framework.exceptions import ValidationError

DEFAULT_PET_IMAGE_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
DEFAULT_MEDICAL_DOC_MAX_SIZE = 15 * 1024 * 1024 # 15 MB

PET_IMAGE_ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MEDICAL_DOC_ALLOWED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png'}

IMAGE_FORMAT_MAPPING = {
    '.jpg': {'JPEG'},
    '.jpeg': {'JPEG'},
    '.png': {'PNG'},
    '.webp': {'WEBP'},
}


def get_pet_image_max_size() -> int:
    try:
        return int(os.getenv('PET_IMAGE_MAX_SIZE_BYTES', DEFAULT_PET_IMAGE_MAX_SIZE))
    except (ValueError, TypeError):
        return DEFAULT_PET_IMAGE_MAX_SIZE


def get_medical_doc_max_size() -> int:
    try:
        return int(os.getenv('MEDICAL_DOC_MAX_SIZE_BYTES', DEFAULT_MEDICAL_DOC_MAX_SIZE))
    except (ValueError, TypeError):
        return DEFAULT_MEDICAL_DOC_MAX_SIZE


def validate_pet_image(uploaded_file):
    """
    Validates pet gallery and profile images:
    - Declared extension must be in {.jpg, .jpeg, .png, .webp}
    - File size <= PET_IMAGE_MAX_SIZE_BYTES (default 10 MB)
    - Content verified with Pillow (Image.verify)
    - Declared extension matches the decoded image format
    - Does NOT trust Content-Type header
    """
    if not uploaded_file:
        raise ValidationError("No image file provided.")

    filename = getattr(uploaded_file, 'name', '') or ''
    ext = Path(filename).suffix.lower()

    if ext not in PET_IMAGE_ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file extension '{ext}'. Allowed image extensions are: {', '.join(sorted(PET_IMAGE_ALLOWED_EXTENSIONS))}."
        )

    max_size = get_pet_image_max_size()
    file_size = getattr(uploaded_file, 'size', None)
    if file_size is None:
        uploaded_file.seek(0, os.SEEK_END)
        file_size = uploaded_file.tell()
        uploaded_file.seek(0)

    if file_size > max_size:
        raise ValidationError(
            f"File size exceeds maximum allowed limit of {max_size // (1024 * 1024)} MB."
        )

    # Verify content with Pillow
    uploaded_file.seek(0)
    try:
        with Image.open(uploaded_file) as img:
            detected_format = img.format
            img.verify()
    except Exception:
        uploaded_file.seek(0)
        raise ValidationError("Invalid image content. File is corrupted or not a valid image.")

    uploaded_file.seek(0)

    expected_formats = IMAGE_FORMAT_MAPPING.get(ext, set())
    if detected_format not in expected_formats:
        raise ValidationError(
            f"Declared file extension '{ext}' does not match actual image content format '{detected_format}'."
        )

    return True


def validate_medical_document(uploaded_file):
    """
    Validates medical records:
    - Declared extension must be in {.pdf, .jpg, .jpeg, .png}
    - File size <= MEDICAL_DOC_MAX_SIZE_BYTES (default 15 MB)
    - Signature check:
      - PDF must start with b'%PDF-'
      - Images must be verified via Pillow and match declared extension
    - Does NOT trust Content-Type header
    """
    if not uploaded_file:
        raise ValidationError("No document file provided.")

    filename = getattr(uploaded_file, 'name', '') or ''
    ext = Path(filename).suffix.lower()

    if ext not in MEDICAL_DOC_ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file extension '{ext}'. Allowed document extensions are: {', '.join(sorted(MEDICAL_DOC_ALLOWED_EXTENSIONS))}."
        )

    max_size = get_medical_doc_max_size()
    file_size = getattr(uploaded_file, 'size', None)
    if file_size is None:
        uploaded_file.seek(0, os.SEEK_END)
        file_size = uploaded_file.tell()
        uploaded_file.seek(0)

    if file_size > max_size:
        raise ValidationError(
            f"File size exceeds maximum allowed limit of {max_size // (1024 * 1024)} MB."
        )

    uploaded_file.seek(0)

    if ext == '.pdf':
        header = uploaded_file.read(5)
        uploaded_file.seek(0)
        if header != b'%PDF-':
            raise ValidationError("Invalid PDF content. File does not contain a valid PDF signature.")
    else:
        # Images: jpg, jpeg, png
        try:
            with Image.open(uploaded_file) as img:
                detected_format = img.format
                img.verify()
        except Exception:
            uploaded_file.seek(0)
            raise ValidationError("Invalid image content for medical document.")

        uploaded_file.seek(0)
        expected_formats = IMAGE_FORMAT_MAPPING.get(ext, set())
        if detected_format not in expected_formats:
            raise ValidationError(
                f"Declared file extension '{ext}' does not match actual image content format '{detected_format}'."
            )

    return True
