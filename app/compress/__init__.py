"""Compression engines for images and PDFs."""

from .images import ImageCompressionError, ImageOptions, ImageResult, compress_image
from .pdfs import PdfCompressionError, PdfOptions, PdfResult, compress_pdf, ghostscript_available
from .utils import KIND_IMAGE, KIND_PDF, human_size, safe_filename, savings_percent, sniff_kind

__all__ = [
    "ImageCompressionError",
    "ImageOptions",
    "ImageResult",
    "compress_image",
    "PdfCompressionError",
    "PdfOptions",
    "PdfResult",
    "compress_pdf",
    "ghostscript_available",
    "KIND_IMAGE",
    "KIND_PDF",
    "human_size",
    "safe_filename",
    "savings_percent",
    "sniff_kind",
]
