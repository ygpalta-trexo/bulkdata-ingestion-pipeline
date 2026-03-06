import io
import os
import lxml.etree as ET

os.makedirs('tmp_dummy', exist_ok=True)
with open('tmp_dummy/docdb-entities.dtd', 'w') as f:
    f.write('<!ENTITY delta "delta_symbol">\n')

xml_data = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE root SYSTEM "docdb-entities.dtd">
<root>
    <tag>&delta;</tag>
</root>
"""
with open('tmp_dummy/test.xml', 'wb') as f:
    f.write(xml_data)

try:
    context = ET.iterparse('tmp_dummy/test.xml', events=("end",), load_dtd=True, no_network=True)
    for event, elem in context:
        print(f"Success natively: {elem.tag} - {elem.text}")
except Exception as e:
    print(f"Failed natively: {e}")
