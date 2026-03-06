from docdb_ingestion.epo_api import download_file
import zipfile
import os

os.makedirs('tmp_inspect', exist_ok=True)
download_file(14, 3071, 8922, 'tmp_inspect/file.zip')
with zipfile.ZipFile('tmp_inspect/file.zip', 'r') as z:
    z.extractall('tmp_inspect/extracted')

for root, _, files in os.walk('tmp_inspect/extracted'):
    for f in files:
        if f.endswith('.zip') and 'DOCDB' in f:
            inner_zip = os.path.join(root, f)
            with zipfile.ZipFile(inner_zip, 'r') as iz:
                for inner_f in iz.infolist():
                    if inner_f.filename.endswith('.xml'):
                        with iz.open(inner_f) as xml:
                            print(xml.read()[:1000].decode('utf-8'))
                            exit(0)
