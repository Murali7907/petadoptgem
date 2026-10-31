import logging
import re
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from .models import PetImage, PetMedicalRecord

logger = logging.getLogger(__name__)


def extract_storage_name(file_ref: str) -> str | None:
    """
    Extracts a local relative storage path or Cloudinary public_id from a stored file reference.
    Returns None if file_ref is None, empty, or an external URL not hosted in our storage.
    """
    if not file_ref or not isinstance(file_ref, str):
        return None
    file_ref = file_ref.strip()
    if not file_ref:
        return None

    # Check for external URLs
    if file_ref.startswith(('http://', 'https://')):
        cloud_name = getattr(settings, 'CLOUDINARY_CLOUD_NAME', '')
        if 'res.cloudinary.com' in file_ref and cloud_name and cloud_name in file_ref:
            # Stored in our Cloudinary account: extract relative path after upload/(v\d+/)?
            m = re.search(r'/upload/(?:v\d+/)?(.+)$', file_ref)
            if m:
                return m.group(1)
        # Any other URL is an external URL (e.g. Unsplash, external CDN)
        return None

    media_url = getattr(settings, 'MEDIA_URL', '/media/')
    if media_url and file_ref.startswith(media_url):
        return file_ref[len(media_url):].lstrip('/')

    if file_ref.startswith('/media/'):
        return file_ref[len('/media/'):].lstrip('/')

    return file_ref.lstrip('/')


@receiver(post_delete, sender=PetImage)
def cleanup_pet_image_file(sender, instance, **kwargs):
    """
    post_delete handler for PetImage that deletes the stored image file from default_storage.
    Scheduled with transaction.on_commit. External URLs are untouched. Storage errors are logged and swallowed.
    """
    storage_name = extract_storage_name(getattr(instance, 'image_url', None))
    if not storage_name:
        return

    def _delete():
        try:
            from django.core.files.storage import default_storage
            default_storage.delete(storage_name)
            logger.info(f"Deleted PetImage file from storage: {storage_name}")
        except Exception as e:
            logger.warning(f"Failed to delete PetImage file {storage_name}: {e}")

    transaction.on_commit(_delete)


@receiver(post_delete, sender=PetMedicalRecord)
def cleanup_pet_medical_record_file(sender, instance, **kwargs):
    """
    post_delete handler for PetMedicalRecord that deletes the attached document file from named/default storage.
    Scheduled with transaction.on_commit. External URLs are untouched. Storage errors are logged and swallowed.
    """
    storage_name = extract_storage_name(getattr(instance, 'document', None))
    if not storage_name:
        return

    def _delete():
        try:
            from apps.pets.storage import get_document_storage
            storage = get_document_storage()

            storage.delete(storage_name)
            logger.info(f"Deleted PetMedicalRecord file from storage: {storage_name}")
        except Exception as e:
            logger.warning(f"Failed to delete PetMedicalRecord file {storage_name}: {e}")

    transaction.on_commit(_delete)
