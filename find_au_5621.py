import zipfile
import lxml.etree as ET
import glob

zips = glob.glob('/home/ygpalta/repos/bdds/docdb_xml_bck_202534_001_A/Root/DOC/DOCDB*-AU-*.zip')
found = False
NS = {'exch': 'http://www.epo.org/exchange'}

for z in zips:
    try:
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if n.endswith('.xml'):
                    data = zf.read(n)
                    if b'>5621<' in data and b'>P<' in data:
                        tree = ET.fromstring(data)
                        for doc in tree.findall('.//exch:exchange-document', namespaces=NS):
                            num = doc.find('.//pub-reference/document-id/doc-number')
                            kind = doc.find('.//pub-reference/document-id/kind')
                            if num is not None and num.text == '5621' and kind is not None and kind.text == 'P':
                                print(f"FOUND IN {z}")
                                biblio = doc.find('.//exch:bibliographic-data', namespaces=NS)
                                if biblio is not None:
                                    print(ET.tostring(biblio, pretty_print=True).decode())
                                found = True
                                break
                if found: break
    except Exception as e:
        pass
    if found: break

if not found:
    print("Not found")
