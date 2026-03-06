"""
Comprehensive tests for extra_data nested field capture in stream_processor.

Tests verify that ALL unhandled fields at ANY nesting level are captured
and properly stored in the ExchangeDocument.pub_master.extra_data field.
"""

import pytest
import lxml.etree as ET
from pathlib import Path
import tempfile
import json
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from docdb_ingestion.stream_processor import xml_to_dict, extract_document_data
from docdb_ingestion.models import ExchangeDocument

NS = {'exch': 'http://www.epo.org/exchange'}


class TestXmlToDict:
    """Tests for the xml_to_dict recursive converter."""

    def test_basic_flat_structure(self):
        """Test simple flat XML with no nesting."""
        xml_str = """
        <root>
            <name>Test</name>
            <value>123</value>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        assert result == {'name': 'Test', 'value': '123'}

    def test_single_level_nesting(self):
        """Test XML with one level of nesting."""
        xml_str = """
        <root>
            <parent>
                <child>value</child>
            </parent>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        assert result == {'parent': {'child': 'value'}}

    def test_deep_nesting(self):
        """Test deeply nested XML (5+ levels)."""
        xml_str = """
        <root>
            <level1>
                <level2>
                    <level3>
                        <level4>
                            <level5>deep_value</level5>
                        </level4>
                    </level3>
                </level2>
            </level1>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        # Navigate through deepnested structure
        assert result['level1']['level2']['level3']['level4']['level5'] == 'deep_value'

    def test_multiple_children_same_tag(self):
        """Test multiple children with same tag name (converts to list)."""
        xml_str = """
        <root>
            <item>first</item>
            <item>second</item>
            <item>third</item>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        assert result['item'] == ['first', 'second', 'third']

    def test_attributes_preserved(self):
        """Test that attributes are captured at all levels."""
        xml_str = """
        <root attr1="root_val">
            <child attr2="child_val">text</child>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        assert result['attr1'] == 'root_val'
        assert result['child']['attr2'] == 'child_val'
        assert result['child']['#text'] == 'text'

    def test_mixed_content_with_text_and_elements(self):
        """Test element with both text and child elements."""
        xml_str = """
        <root>
            some text
            <child>value</child>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        # Should capture both text and child element
        assert result['#text'] == 'some text'
        assert result['child'] == 'value'

    def test_namespaced_elements(self):
        """Test that namespace prefixes are stripped."""
        xml_str = f"""
        <root xmlns:exch="{NS['exch']}">
            <exch:parent>
                <exch:child>value</exch:child>
            </exch:parent>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        # Namespace prefixes should be stripped
        assert 'parent' in result
        assert result['parent']['child'] == 'value'

    def test_empty_elements(self):
        """Test handling of empty elements."""
        xml_str = """
        <root>
            <empty></empty>
            <nonempty>value</nonempty>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        # Empty elements return None, which is included in dict
        assert result['nonempty'] == 'value'
        assert result['empty'] is None

    def test_nested_lists(self):
        """Test nested structures with multiple items."""
        xml_str = """
        <root>
            <parent>
                <item>item1</item>
                <item>item2</item>
            </parent>
            <parent>
                <item>item3</item>
            </parent>
        </root>
        """
        root = ET.fromstring(xml_str)
        result = xml_to_dict(root)
        
        # Multiple parents should be a list
        assert isinstance(result['parent'], list)
        assert len(result['parent']) == 2
        # Each parent's items should be lists
        assert result['parent'][0]['item'] == ['item1', 'item2']
        assert result['parent'][1]['item'] == 'item3'

    def test_max_depth_protection(self):
        """Test that max_depth parameter prevents infinite recursion."""
        # Create deeply nested XML
        xml_str = "<root>" + "<child>" * 60 + "value" + "</child>" * 60 + "</root>"
        root = ET.fromstring(xml_str)
        
        # Should not crash with max_depth limit
        result = xml_to_dict(root, max_depth=50)
        assert result is not None  # Should get partial result, not crash


class TestExtraDataCapture:
    """Tests for extra_data capture in extract_document_data."""

    def create_minimal_exchange_document(self, extra_xml_str=""):
        """Helper to create a minimal valid exchange-document XML with optional extra fields."""
        xml_str = f"""<exchange-document 
            xmlns="http://www.epo.org/exchange"
            country="US" 
            doc-number="123456" 
            kind="B1" 
            date-publ="20200101"
            doc-id="US123456B1"
            family-id="FAM123"
            status="C">
            
            <application-reference data-format="docdb">
                <document-id>
                    <country>US</country>
                    <doc-number>123456</doc-number>
                    <kind>A</kind>
                    <date>20190101</date>
                </document-id>
            </application-reference>
            
            {extra_xml_str}
        </exchange-document>
        """
        return ET.fromstring(xml_str)

    def test_no_extra_data_minimal_document(self):
        """Test that only known fields are captured when no extras exist."""
        elem = self.create_minimal_exchange_document()
        doc = extract_document_data(elem)
        
        assert isinstance(doc.pub_master.extra_data, dict)
        # Should have minimal or no extra data
        assert len(doc.pub_master.extra_data) == 0

    def test_single_unhandled_field(self):
        """Test capture of a single unhandled field at root level."""
        extra_xml = """
        <custom-field>custom_value</custom-field>
        """
        elem = self.create_minimal_exchange_document(extra_xml)
        doc = extract_document_data(elem)
        
        assert 'custom-field' in doc.pub_master.extra_data
        assert doc.pub_master.extra_data['custom-field'] == 'custom_value'

    def test_multiple_unhandled_fields(self):
        """Test capture of multiple unhandled fields at root level."""
        extra_xml = """
        <custom-field1>value1</custom-field1>
        <custom-field2>value2</custom-field2>
        <custom-field3>value3</custom-field3>
        """
        elem = self.create_minimal_exchange_document(extra_xml)
        doc = extract_document_data(elem)
        
        assert doc.pub_master.extra_data['custom-field1'] == 'value1'
        assert doc.pub_master.extra_data['custom-field2'] == 'value2'
        assert doc.pub_master.extra_data['custom-field3'] == 'value3'

    def test_deeply_nested_unhandled_data(self):
        """Test capture of unhandled data with deep nesting."""
        extra_xml = """
        <custom-block>
            <level1>
                <level2>
                    <level3>
                        <deep-field>deep_value</deep-field>
                    </level3>
                </level2>
            </level1>
        </custom-block>
        """
        elem = self.create_minimal_exchange_document(extra_xml)
        doc = extract_document_data(elem)
        
        # Deeply nested data should be preserved
        assert 'custom-block' in doc.pub_master.extra_data
        extra = doc.pub_master.extra_data['custom-block']
        assert extra['level1']['level2']['level3']['deep-field'] == 'deep_value'

    def test_nested_lists_in_extra_data(self):
        """Test capture of repeated elements as lists in extra_data."""
        extra_xml = """
        <custom-items>
            <custom-item sequence="1">item_value_1</custom-item>
            <custom-item sequence="2">item_value_2</custom-item>
            <custom-item sequence="3">item_value_3</custom-item>
        </custom-items>
        """
        elem = self.create_minimal_exchange_document(extra_xml)
        doc = extract_document_data(elem)
        
        # Items should be captured as list
        assert 'custom-items' in doc.pub_master.extra_data
        items = doc.pub_master.extra_data['custom-items']['custom-item']
        assert isinstance(items, list)
        assert len(items) == 3
        assert items[0]['sequence'] == '1'
        assert items[1]['#text'] == 'item_value_2'

    def test_unhandled_data_with_attributes(self):
        """Test that attributes in unhandled data are preserved."""
        extra_xml = """
        <custom-element attr1="val1" attr2="val2">
            <nested attr3="val3">nested_value</nested>
        </custom-element>
        """
        elem = self.create_minimal_exchange_document(extra_xml)
        doc = extract_document_data(elem)
        
        custom = doc.pub_master.extra_data['custom-element']
        assert custom['attr1'] == 'val1'
        assert custom['attr2'] == 'val2'
        assert custom['nested']['attr3'] == 'val3'
        assert custom['nested']['#text'] == 'nested_value'

    def test_known_blocks_excluded(self):
        """Test that known/handled blocks are NOT in extra_data."""
        extra_xml = """
        <custom-field>should_be_included</custom-field>
        """
        elem = self.create_minimal_exchange_document(extra_xml)
        doc = extract_document_data(elem)
        
        # Known blocks should NOT be in extra_data (hyphens preserved in tag names)
        known_blocks = [
            'priority-claims', 'parties', 'designation-epc', 
            'designation-pct', 'patent-classifications', 'references-cited',
            'dates-of-public-availability', 'abstract', 'invention-title'
        ]
        
        for block in known_blocks:
            assert block not in doc.pub_master.extra_data
        
        # But custom field should be there
        assert 'custom-field' in doc.pub_master.extra_data

    def test_extra_data_json_serializable(self):
        """Test that extra_data can be serialized to JSON."""
        extra_xml = """
        <complex-structure>
            <nested>
                <field1>value1</field1>
                <field2>value2</field2>
            </nested>
            <items>
                <item>item1</item>
                <item>item2</item>
            </items>
        </complex-structure>
        """
        elem = self.create_minimal_exchange_document(extra_xml)
        doc = extract_document_data(elem)
        
        # Should be JSON serializable
        try:
            json_str = json.dumps(doc.pub_master.extra_data)
            # Should also deserialize back
            deserialized = json.loads(json_str)
            assert deserialized == doc.pub_master.extra_data
        except TypeError as e:
            pytest.fail(f"extra_data is not JSON serializable: {e}")

    def test_mixed_handled_and_unhandled_data(self):
        """Test document with both handled and unhandled blocks."""
        extra_xml = """
        <custom-vendor-field>custom_value</custom-vendor-field>
        <patent-classifications>
            <patent-classification sequence="1">
                <classification-symbol>H04L</classification-symbol>
            </patent-classification>
        </patent-classifications>
        <custom-other-field>other_value</custom-other-field>
        """
        elem = self.create_minimal_exchange_document(extra_xml)
        doc = extract_document_data(elem)
        
        # Unhandled namespaced patent-classifications won't be parsed (needs exch: namespace)
        # But they also shouldn't appear in extra_data because they're in the known_blocks list
        # Check that custom fields ARE in extra_data
        assert 'custom-vendor-field' in doc.pub_master.extra_data
        assert 'custom-other-field' in doc.pub_master.extra_data
        assert doc.pub_master.extra_data['custom-vendor-field'] == 'custom_value'
        assert doc.pub_master.extra_data['custom-other-field'] == 'other_value'

    def test_extra_data_none_vs_empty_dict(self):
        """Test that extra_data is always a dict, never None."""
        elem = self.create_minimal_exchange_document()
        doc = extract_document_data(elem)
        
        assert doc.pub_master.extra_data is not None
        assert isinstance(doc.pub_master.extra_data, dict)


class TestRealWorldScenarios:
    """Tests with more realistic XML structures."""

    def test_vendor_specific_extension_fields(self):
        """Test capture of vendor-specific extension fields."""
        xml_str = f"""<exchange-document 
            xmlns="http://www.epo.org/exchange"
            xmlns:vendor="http://example.com/vendor-ext"
            country="EP" 
            doc-number="3000000" 
            kind="A1" 
            date-publ="20230101"
            doc-id="EP3000000A1"
            family-id="FAM001"
            status="C">
            
            <application-reference data-format="docdb">
                <document-id>
                    <country>EP</country>
                    <doc-number>3000000</doc-number>
                    <kind>A</kind>
                    <date>20220101</date>
                </document-id>
            </application-reference>
            
            <vendor:custom-metadata>
                <vendor:processing-info>
                    <vendor:priority>HIGH</vendor:priority>
                    <vendor:batch-id>BATCH_2023_001</vendor:batch-id>
                    <vendor:quality-score>0.95</vendor:quality-score>
                </vendor:processing-info>
            </vendor:custom-metadata>
        </exchange-document>
        """
        
        elem = ET.fromstring(xml_str)
        doc = extract_document_data(elem)
        
        # Should capture vendor extension (tag names preserve hyphens)
        assert 'custom-metadata' in doc.pub_master.extra_data
        metadata = doc.pub_master.extra_data['custom-metadata']
        assert metadata['processing-info']['priority'] == 'HIGH'
        assert metadata['processing-info']['batch-id'] == 'BATCH_2023_001'
        assert metadata['processing-info']['quality-score'] == '0.95'

    def test_future_epo_format_changes(self):
        """Test that new EPO format additions are captured."""
        xml_str = f"""<exchange-document 
            xmlns="http://www.epo.org/exchange"
            country="WO" 
            doc-number="2023000001" 
            kind="A1" 
            date-publ="20230601"
            doc-id="WO2023000001A1"
            family-id="FAM_WO"
            status="C">
            
            <application-reference data-format="docdb">
                <document-id>
                    <country>WO</country>
                    <doc-number>2023000001</doc-number>
                    <kind>A</kind>
                    <date>20221201</date>
                </document-id>
            </application-reference>
            
            <new-future-field>
                <future-subfield>future_value</future-subfield>
            </new-future-field>
        </exchange-document>
        """
        
        elem = ET.fromstring(xml_str)
        doc = extract_document_data(elem)
        
        # Should capture unknown future fields (tag names preserve hyphens)
        assert 'new-future-field' in doc.pub_master.extra_data
        assert doc.pub_master.extra_data['new-future-field']['future-subfield'] == 'future_value'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
