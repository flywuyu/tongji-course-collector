"""Image-only PDFs with permissive dependencies and bounded decoded-image memory."""
import io
import os
from pathlib import Path
from PIL import Image


def make_image_pdf(paths,destination):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    destination=str(destination)
    temporary=destination+'.tmp'
    writer=canvas.Canvas(temporary,pageCompression=1)
    try:
        for path in paths:
            with Image.open(path) as source:
                rgb=source.convert('RGB')
                try:
                    with io.BytesIO() as encoded:
                        rgb.save(encoded,format='JPEG',quality=90)
                        encoded.seek(0)
                        width,height=rgb.width*.5,rgb.height*.5
                        writer.setPageSize((width,height))
                        writer.drawImage(ImageReader(encoded),0,0,width=width,height=height)
                        writer.showPage()
                finally:
                    rgb.close()
        writer.save()
        os.replace(temporary,destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def page_count(path):
    from pypdf import PdfReader
    with open(path,'rb') as stream:
        return len(PdfReader(stream).pages)
