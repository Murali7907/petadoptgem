"""
Storage access utilities for pet media and medical documents.
"""
from django.core.files.storage import storages, default_storage


def get_document_storage():
    """
    Returns the named storage for raw medical documents ('raw_documents').
    When USE_CLOUDINARY=True, this uses RawMediaCloudinaryStorage (resource_type='raw'),
    preserving PDFs, DOCX, and raw binaries without image rasterization.
    Falls back to default_storage if 'raw_documents' is not configured.
    """
    try:
        return storages['raw_documents']
    except Exception:
        return default_storage
