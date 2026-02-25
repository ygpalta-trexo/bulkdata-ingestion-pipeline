# **Architectural Blueprint for Integrating the EPO DOCDB XML Exchange Format**

# **Introduction to the European Patent Office Master Documentation Database**

The European Patent Office (EPO) maintains and curates the world’s most formidable and authoritative collection of intellectual property information within its master documentation database, universally recognized as DOCDB.1 This repository functions as the central nervous system for global patent data, aggregating, standardizing, and perpetually enriching bibliographic records, technical abstracts, and highly complex cited reference networks from over seventy national and regional patent authorities globally.1 To disseminate this massive, constantly shifting repository to trilateral partners, commercial data providers, and external database integrators, the EPO utilizes an advanced Extensible Markup Language (XML) format.1 This format is fundamentally based on the World Intellectual Property Organization (WIPO) Standard ST.36; however, it incorporates highly specific, proprietary extensions to manage the unique demands of global database synchronization.1

The DOCDB XML format transcends the basic parameters of the WIPO ST.36 publication schema. While standard ST.36 is typically designed to accommodate the linear lifecycle of a single patent application within a singular national office, the DOCDB XML is structurally engineered for large-scale, asynchronous database exchange across disparate global jurisdictions.1 Consequently, the root element of the payload utilizes the \<exch:exchange-document\> nomenclature rather than a localized \<xx-patent-document\> tag.1 This deliberate architectural distinction reflects the highly enriched nature of the data, which includes multiple concurrent representations of a single entity, normalized cross-jurisdictional classifications, and structured citation networks that contextualize an individual patent within the broader global technological landscape.1

Integrating this data stream into a localized relational database architecture requires a profound understanding of the XML hierarchy, the sequencing of data deliveries, the implementation of EPO surrogate keys, and the intricate behavioral logic of document-level and element-level status indicators.1 The following analysis provides an exhaustive, multi-layered architectural blueprint for consuming, parsing, modeling, and persisting DOCDB XML data accurately.

## **Data Formats and the Paradigm of Multiple Representations**

A central paradigm of the DOCDB data model is the native support for multiple parallel representations of identical data points, governed structurally by the data-format attribute.1 The necessity for multiple concurrent formats arises from the global nature of the data pool, where varying transliteration standards, disparate national publication practices, and historical legacy systems demand differing levels of data normalization.1

The DOCDB XML schema actively utilizes four primary data formats, each carrying distinct processing implications for database ingestion:

1. **The "docdb" Format:** This format represents data that has been strictly normalized against the EPO’s prime standardization rules.1 When applied to document identifiers, the format strictly delineates the country, number, and kind code into separate discrete XML tags (\<country\>, \<doc-number\>, \<kind\>).1 For entity names (such as applicants or inventors), the "docdb" format provides the formalized, standardized version of the name, usually restricted to the name itself and the country of residence, stripping away unstructured address data to ensure high-fidelity relational indexing.1  
2. **The "epodoc" Format:** This format represents alphanumeric document identifiers concatenated into a single, continuous string (combining the country code, a padded numeric series, and optionally the kind code) to exactly mimic the structure utilized by the public Espacenet search interface.1 For example, an application identified as WO (country), 2018100189 (number), and W (kind) in "docdb" format will be transformed "on the fly" by the EPO extraction engine into WO2018CN100189 under the "epodoc" format.1 This format is explicitly generated at exchange time, and if formatting errors occur during this dynamic generation, the "epodoc" representation may be temporarily omitted from the delivery.1  
3. **The "docdba" Format:** This format preserves data—specifically inventor names, applicant entities, and abstracts—in the Latin character set exactly as supplied by the source authority, prior to deep EPO formalization.1 When parsing entity data, the "docdba" format is highly valuable as it frequently includes unformatted, free-text address data populated within a \<text\> element that the "docdb" format aggressively filters out.1  
4. **The "original" Format:** This format retains the data in its native, original language character set as provided by the national office.1 This is critical for Asian jurisdictions (e.g., preserving Kanji, Hiragana, Hangul, or Chinese characters) and Cyrillic records.1 The EPO ensures that all original language characters are exchanged strictly as UTF-8 numeric entities (e.g., З) to guarantee universal transferability and prevent character degradation during XML parsing.1

When parsing entities that utilize these formats, the sequence attribute is deployed to maintain structural indexing order.1 It is analytically critical to recognize that the enumeration of the sequence attribute restarts at 1 with every change in the data-format attribute.1 Therefore, an ingestion algorithm processing an \<exch:applicant\> with sequence="1" and data-format="docdb" cannot blindly assume absolute entity equivalency with an applicant marked sequence="1" under data-format="original".1 The EPO explicitly states that it does not guarantee the consistency of sorting orders across differently standardized sets, largely because the EPO only corrects and manipulates "docdb" data to maintain quality, leaving "docdba" and "original" untouched.1 The only guaranteed consistency in sequence numbering is the direct bridge between the "docdb" and "epodoc" formats for document identifiers.1

## **System Keys and Unique Identifiers**

To construct a robust database, architects must understand how the EPO identifies unique entities. Historically, the master databases were keyed on the natural publication-identifier: the combination of country, publication number, and publication kind-code.1 However, this natural key paradigm proved insufficient for the complexities of modern patent tracking.

The primary point of failure for natural keys in patent data involves legitimate duplicates.1 Certain publication events result in the issuance of multiple documents sharing the exact same identifier. Examples include modifications of the full specification (e.g., kind codes A8, B8, U8), modifications restricted to the first page (B8, B9, U9), republications following limitation procedures (EP-B3), or the later publication of International Search Reports with a revised front page (WO-A3).1 In these instances, the publication date technically defines the unique instance. However, utilizing the publication date as part of a primary database key introduces catastrophic instability, as publication dates are frequently subjected to retroactive correction by the EPO.1 If the date is part of the key, a correction triggers the creation of an undesirable duplicate in the target database rather than updating the existing record.1

To permanently resolve this architectural vulnerability, the EPO introduced the doc-id attribute.1

### **The Implementation of doc-id**

The doc-id is a highly strategic, variable-length (maximum 10-digit) numerical surrogate key that uniquely and stably identifies an entity across its entire lifecycle.1 The schema utilizes the doc-id attribute in two entirely distinct scopes, each mapping to a different series of integers:

1. **Publication doc-id:** Located directly on the \<exchange-document\> root element (and mirrored inside the nested \<publication-reference\> block), this integer uniquely identifies the physical publication event.1 Every patent publication, with the explicit exception of "void" documents, receives a stable publication doc-id.1 This completely resolves the duplicate identifier issue; two documents with the identical country, number, and kind code (e.g., two EP-B3s) will possess distinct, unique doc-id values.1  
2. **Application doc-id:** Located on the \<application-reference\> element, this distinct integer uniquely identifies the underlying filing event.1 The stable and unique identifier contained here allows downstream systems to reliably link multiple subsequent publications back to their single foundational application.1 It is also populated within \<priority-claims\> if the claimed priority is present within DOCDB as a formal application, allowing for flawless relational graphing of priority lineages.1

### **Exotic and Dummy Identifiers**

The EPO data flow is designed to prioritize the ingestion of bibliographic data even when official identifiers are temporarily missing or malformed.1 To achieve this, DOCDB employs "exotic" kind codes and number suffixes that downstream databases must be programmed to handle.

* **Suffixes 'D', 'T', and 'X':** The EPO maintains records of highly historical publications (pre-1920s) and paper-only documents.1 When an application number is unknown, the EPO derives a "dummy" application number by appending a 'D' to the publication number.1 Similarly, missing priority numbers are simulated by appending a 'T' (Technical priority) to a known family document number, or an 'X' to historical publications where only the priority date and country were originally printed.1 Documents bearing these dummy suffixes frequently lack application or priority date fields entirely.1  
* **Exotic Kind Codes ('K', 'L', 'M', 'N', 'D', 'Q'):** Certain national offices (e.g., RU, SU, PH) occasionally supply identical application identifiers for completely distinct patents.1 To force these into the database without collision, the EPO assigns sequential exotic kind codes ('K' for the first duplicate, 'L' for the second).1 Kind codes 'D' and 'Q' represent dummy applications awaiting automated or manual intellectual resolution.1

## **Fetching, Processing, and Updating Logic**

Integrating the DOCDB XML into a live relational database demands absolute adherence to the EPO’s prescribed sequence of processing rules, driven by the status attributes located at the document level and the element level.1

### **Document-Level Status and the Re-Key Paradigm**

A patent publication is triggered for transmission in the weekly exchange payload under specific conditions: it is newly added to the database, removed or withdrawn, subjected to a modification of its underlying data, or re-keyed (meaning the foundational identifier itself has been altered).1 The physical action performed on the master database is broadcast via the status attribute on the \<exch:exchange-document\> root element.1

* **Status 'C' (Create):** The publication has been newly added. The XML contains a complete bibliographic image.1  
* **Status 'D' (Delete):** The publication has been purged. The XML will *only* contain the bare publication identifier (country, number, kind), entirely lacking publication dates, applications, or bibliographic payload.1  
* **Status 'A' (Amend):** The publication has been updated. The XML provides the complete, refreshed bibliographic image.1  
* **Status 'CV' and 'DV' (Create Void / Delete Void):** These statuses indicate that a record of a publication being legally withdrawn has been inserted or deleted.1 Similar to 'D' records, withdrawn publications are exchanged as identifier-only stubs.1

The most precarious integration challenge for database administrators involves the "Publication Re-Key" paradigm.1 A re-key occurs when the defining identifier components of a document are retroactively altered, or when the stable doc-id itself requires realignment due to manual intellectual intervention.1 When an identifier is re-keyed, the EPO emits two distinct records within the weekly exchange: a Status 'D' payload commanding the deletion of the deprecated identifier, and a corresponding Status 'C' payload carrying the corrected identifier and the complete data image.1

Because XML packages are physically split by file size (to prevent unmanageable massive files), these paired 'D' and 'C' records are not guaranteed to reside within the same physical XML file.1 Furthermore, they may not appear sequentially.1 To prevent catastrophic data corruption—such as an ingestion pipeline processing the 'C' record before the 'D' record, thereby temporarily duplicating the entity before erroneously deleting the newly created instance—the EPO segregates high-risk data into dedicated, specifically named directories.1

The ingestion pipeline must execute in a strictly synchronous mathematical order, effectively an ![][image1] sequential processing flow:

1. **Parse DeleteRekey Archives:** Execute all deletion transactions contained in files matching the nomenclature DOCDB-\*-DeleteRekey-PubDate\*AndBefore-\*.1 These files contain the 'D' halves of complex re-keys where the string identifier remained identical but the doc-id was mutated.1  
2. **Parse CreateDelete Archives:** Process all files matching DOCDB-\*-CreateDelete-PubDate\*AndBefore-\*.1 Within these files, the system must independently route 'D' statuses to execute deletions first, followed subsequently by processing 'C' statuses to insert the new records.1  
3. **Parse Amend Archives:** Finally, process files matching DOCDB-\*-Amend-\*, executing UPSERT (Update/Insert) operations against the database.1

### **The Element-Level Status Indicator and Wipe-and-Load Methodology**

For documents flagged with an 'A' (Amend) status at the document level, the EPO attempts to optimize downstream delta-detection by utilizing a field-level change indicator.1 This manifests as status="A" populated on specific sub-elements within the XML hierarchy.1

However, the behavioral logic of this element-level status is holistic to the structural "data-unit" rather than the individual XML node. A data-unit encompasses the entire iterable block for a specific entity type.1 For example, if a single inventor's name is corrected among a list of ten inventors, every single \<exch:inventor\> tag within that document will be broadcast with status="A".1

Therefore, the correct database operation upon encountering an element-level status="A" is not to attempt a surgical primary-key update on a single row. Instead, the database must execute a complete transactional wipe (a hard delete) of all existing records related to that specific data-unit for that specific publication, followed immediately by a bulk INSERT of the newly provided XML nodes.1

Furthermore, data engineers must be aware that data elements generated dynamically "on the fly" by the EPO extraction engine—such as the epodoc data formats, the computed dates of public availability, or the entire \<patent-family\> construct—will never carry element-level status flags.1 Changes to these calculated fields do not constitute a physical update within the master database, meaning they bypass the field-level change indicator entirely.1 Consequently, systems must rely on programmatic hashing or total document overwrites to detect changes in these dynamic fields.

## **Exhaustive Analysis of Bibliographic Data Elements**

The core of the patent payload resides within the \<exch:bibliographic-data\> element, which encapsulates the technical metadata defining the patent's lifecycle, jurisdictional scope, intellectual provenance, and structural categorization.1

### **Core References and Milestones**

**1\. \<exch:publication-reference\> and \<exch:application-reference\>:** As detailed previously, these elements carry the foundational identifiers and doc-id keys.1 The application reference also carries the is-representative boolean.1

**2\. \<exch:previously-filed-app\>:** Introduced to comply with EPC 2000 requirements (INID code 27), this tag captures the previously filed application.1 The EPO stores this exactly as provided by the supplier without reformatting, outputting it as a concatenated string of country, number, and date separated by spaces.1 It is crucial to note that this does not legally represent an additional priority claim; if the previously filed application is also formally claimed as a priority, it will be duplicated within the \<exch:priority-claims\> element.1

**3\. \<exch:preceding-publication-date\> and \<exch:date-of-coming-into-force\>:** These highly specific milestones capture historical lifecycle data. The date of coming into force is almost exclusively utilized for German (DE) utility models, capturing the *Bekanntmachungstag* (announcement day) while the primary publication date tag holds the *Eintragungstag* (registration day).1

**4\. \<exch:extended-kind-code\>:** While the standard kind code (e.g., A1, B2) defines the publication level, documents originating from Germany (DE) and the World Intellectual Property Organization (WO) utilize highly granular extended kind codes.1 Exchanged as a numerical or alphanumeric string, these codes denote highly specific legal statuses.1 For example, an older WO document might carry an extended code of "130100," where the '1' in the first position indicates publication with an international search report, and the '3' in the second position indicates publication before the expiration of the time limit for amending claims.1

### **Patent Classifications: A Multi-Scheme Taxonomy**

The classification of technical subject matter within the DOCDB XML is exceptionally sophisticated, utilizing the \<exch:patent-classifications\> grouping element to support a multi-scheme taxonomy.1 Obsolete schemes such as ECLA (European Classification) and ICO have been permanently deprecated and replaced within the XML schema.1

The dominant classification scheme is the Cooperative Patent Classification International (CPCI), which officially incorporated and superseded the transitional CPC and CPCNO classifications.1 The CPCI picture is maintained strictly at the family level by the EPO; therefore, a change to the CPCI allocations for a single patent will trigger an exchange payload for every single member of that DOCDB simple patent family simultaneously.1

Because CPCI symbols are allocated globally by multiple authorities (e.g., the EPO, the USPTO, the Chinese patent office), duplicate symbols may exist on a single document. These are made unique via the \<generating-office\> tag.1 The schema distinguishes the core inventive focus using the \<classification-value\> attribute ('I' for Invention, 'A' for Additional information), and utilizes the \<symbol-position\> attribute ('F' for First, 'L' for Later) to denote the primary classification chronologically confirmed by an authorized examiner.1

**Combination Sets:** A vital architectural component of the CPCI scheme is the \<combination-set\> element.1 A combination set corresponds to the classification of technical features that function interdependently within the same specific embodiment of an invention (akin to a recipe requiring multiple ingredients to function).1 These are hierarchically modeled using a \<group-number\> to define the overarching cluster, which contains multiple \<combination-rank\> elements sequentially ordered by a \<rank-number\>, each holding a distinct \<patent-classification\> symbol.1 Relational database mapping requires a sophisticated table architecture to accurately preserve this sequential grouping, which is strictly required for semantic search integrity.1

Beyond CPCI, the XML schema natively supports and exchanges the International Patent Classification across its various iterations. IPC versions 1 through 7 are captured in \<exch:classification-ipc\>, breaking down the components into main-classification, further-classification, and deeply structured linked-indexing-code-group elements.1 IPC version 8 is captured in \<exch:classification-ipcr\>, conforming to the WIPO ST.8 standard and outputting the data as a continuous 50-byte string within a \<text\> element.1 Finally, localized national classifications, including the United States Classification (DOCUS) and Japanese classifications (FI and FTERM), are exchanged exactly as supplied by the national offices without EPO quality control.1

### **Priority Claims and the Simple Patent Family Mechanism**

Intellectual property lineage is established via the \<exch:priority-claims\> element.1 This element houses multiple \<exch:priority-claim\> blocks representing the foundational filings providing the document's novelty dates.1

The element contains highly specialized sub-tags. The \<exch:priority-linkage-type\> utilizes a standardized single-byte indicator to define the specific procedural relationship (e.g., 'A' for standard addition, 'W' for PCT applications, 'T' for generated technical links).1

Critically, the \<exch:priority-active-indicator\> utilizes a boolean 'Y' or 'N'.1 This flag is purely for internal EPO family-building algorithms. The DOCDB simple patent family concept groups applications together that share identical technical content.1 The automated heuristic engine evaluates the priority picture, tagging priorities as "Active" ('Y') if they add new technical detail (e.g., first filings, continuations-in-part).1 Priorities that do not add novel technical detail are flagged "Not Active" ('N') and are excluded from the family logic.1 To further facilitate this logic, the EPO frequently generates "self-claims" (dummy priorities where the application claims itself) to force the system to recognize the document as a foundational first filing.1

The culmination of this logic is the \<patent-family\> element, branching from the main \<exch:exchange-document\> root and tied to the 9-digit family-id.1 Within this element, each \<exch:family-member\> replicates the application and publication identifiers of sibling documents, providing a pre-compiled registry of all equivalent publications.1 However, the family-id is a derived, non-functional key. If an underlying priority claim is corrected or its active status changes, a publication will seamlessly detach from its source family and dynamically migrate to a target family.1 The daily exchange delivery provides only the new configuration of the target family; therefore, local relational databases must treat the family-id as a mutable attribute tied temporally to the most recent update.1

### **Parties and Jurisdictional Designations**

Entities holding intellectual or financial stakes are delineated within the \<exch:parties\> element, separated logically into \<exch:applicants\> and \<exch:inventors\>.1 When utilizing data-format="docdb", the name is strictly normalized, and address information is constrained to the country of residence.1 Under data-format="docdba", unformatted, free-text address strings are provided.1

The geographical scope of the patent's protection is detailed in the \<exch:designation-of-states\> element.1 This area is highly granular, splitting into PCT designations (\<exch:designation-pct\>) and European designations (\<exch:designation-epc\>).1 For PCT filings, states are divided into \<exch:regional\> clusters (e.g., the ARIPO or Eurasian regions) and specific \<exch:national\> targets.1 For European filings, the XML explicitly breaks down the jurisdictional intent into \<contracting-states\>, \<extension-states\>, \<validation-states\>, and \<up-participating-states\> (states participating in the Unitary Patent framework).1

### **Titles, Abstracts, and Dates of Public Availability**

Titles (\<exch:invention-title\>) and Abstracts (\<exch:abstract\>) rely on the lang attribute to specify the ISO 639 standard language code (e.g., 'en', 'de', 'ja').1 A single publication may contain multiple abstracts derived from different sources. The abstract-source attribute differentiates between texts provided directly by the "national office", "translation" texts, automated "transcript" texts, texts provided directly by the "EPO", or highly specialized datasets such as "PAJ" (Patent Abstracts of Japan).1 The EPO actively attempts to source and attach an English language abstract to the overall simple patent family, dynamically inserting the best available translation or transcript.1

The publication date is duplicated and further categorized within the \<exch:dates-of-public-availability\> element.1 This element maps the event to specific procedural milestones, offering categories ranging from \<gazette-reference\> to \<unexamined-printed-without-grant\> or \<modified-complete-spec-pub\>.1 Because these nodes are generated dynamically during the XML export based on the kind-code concordance, they do not possess a status attribute and must be fully overwritten upon ingestion.1

### **Rich Citations and the Prior Art Network**

The \<exch:references-cited\> element houses the comprehensive network of prior art.1 With the rollout of DOCDB XML Version 2.5, the EPO introduced "Rich Citation Data," drastically enhancing the structural granularity by migrating away from the historic limitation of 99 citations per document and attaching citations directly to their citing publication rather than consolidating them at the application level.1

The root of each reference is the \<exch:citation\> element, dictated by the cited-phase attribute, which explicitly details the procedural origin of the citation.1 The phases include Pre-Grant/Pre-Search (PRS), Applicant citations (APP), the formal Search Report (SEA), Examination (EXA), and the adversarial phases of Opposition (OPP), Filed for Opposition (FOP), and Third Party Observations (TPO).1 For adversarial phases like TPO or FOP, a name attribute records the identity of the opposing party.1 (It is important to note the legal distinction: FOP documents are submitted by opponents and are attached to the first publication step, while OPP documents are the final selection made by the Opposition Division and are printed directly on the B2 grant document).1

Citations are structurally divided into patent literature (\<patcit\>) and non-patent literature (\<nplcit\>).1

* **Patent Citations:** These identify the targeted document using the dnum attribute alongside the unique doc-id surrogate key.1 The dnum-type attribute resolves ambiguities regarding whether the citation points to a published grant or an unpublished application.1  
* **Non-Patent Literature:** These utilize the npl-type attribute to indicate formatting (e.g., 'b' for books, 's' for serials/journals, 'c' for chemical abstracts, 'e' for databases).1 The citation text is housed within the \<text\> element, often preserving numeric entities for Asian character sets. To facilitate rapid indexing, the EPO algorithms extract reference numbers into the highly useful extracted-xp attribute, and patent numbers embedded within NPL texts are pulled into a \<source-doc\> tag.1  
* **Corresponding Documents:** The \<exch:corresponding-docs\> element handles "ampersand documents" (&-documents), which are members of the same patent family believed to be substantially identical to the inspected document.1

The relevance of the prior art is structurally bound to specific claims within the document via the \<rel-passage\> element.1 This block defines the exact location of the prior art using the \<passage\> tag, links it to specific patent claims using the \<rel-claims\> tag, and assigns a qualitative relevance indicator via the \<category\> tag.1 Categories denote severity, ranging from 'X' (prejudicing novelty independently), 'I' (prejudicing inventive step independently), 'Y' (prejudicing inventive step when combined), 'A' (defining state of the art), to 'R' (same-day filings found in Chinese publications).1 For historical data (pre-1994) lacking rich markup, the EPO simulates this structure by providing empty \<passage\> and \<rel-claims\> tags alongside the populated \<category\>.1

## **Database Schema Design and Storage Strategy**

Translating the deeply hierarchical, multi-formatted XML schema of DOCDB into a normalized relational database requires abstracting the surrogate keys and establishing rigorous referential integrity based on the doc\_id architecture.1 The following schema tables are designed to comprehensively ingest the Version 2.5.9 payload, adhering to the "wipe and load" methodology required by the element-level status flags.

### **1\. Document Master Table**

This table acts as the primary anchor. It utilizes the publication's doc-id as the primary key. It explicitly avoids using the publication date in any unique key constraints to prevent duplication upon retroactive EPO corrections.1

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| pub\_doc\_id | BIGINT | **Primary Key**. 10-digit unique publication identifier.1 |
| country | VARCHAR(2) | WIPO ST.3 publishing country code.1 |
| doc\_number | VARCHAR(20) | Maximum 15 digit alphanumeric identifier.1 |
| kind\_code | VARCHAR(5) | Publication kind code.1 |
| extended\_kind | VARCHAR(10) | Deep granularity code utilized predominantly by DE/WO.1 |
| date\_publ | DATE | Formatted from CCYYMMDD.1 |
| family\_id | BIGINT | 9-digit key linking to the Simple Patent Family.1 |
| is\_representative | BOOLEAN | Denotes family representative status ('YES'/'NO').1 |
| exchange\_status | VARCHAR(2) | Current document status (C, D, A, CV, DV).1 |

### **2\. Application Reference Table**

Records the foundational filing details. A single application event can yield multiple publications.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| app\_doc\_id | BIGINT | **Primary Key**. 10-digit application surrogate key.1 |
| pub\_doc\_id | BIGINT | **Foreign Key** referencing Document Master.1 |
| format\_type | VARCHAR(10) | 'docdb', 'epodoc', or 'original'.1 |
| app\_country | VARCHAR(2) | Filing jurisdiction.1 |
| app\_number | VARCHAR(20) | Application number.1 |
| app\_kind\_code | VARCHAR(5) | Application kind (e.g., 'W' for PCT, 'D'/'Q' for dummy).1 |
| app\_date | DATE | Filing date (NULL for dummy applications).1 |

### **3\. Priority Claims Table**

Maps the timeline of intellectual precedence and the metrics used for simple patent family building.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| id | BIGINT | **Primary Key**, Auto-incrementing surrogate. |
| pub\_doc\_id | BIGINT | **Foreign Key** referencing Document Master.1 |
| format\_type | VARCHAR(10) | 'docdb', 'epodoc', or 'original'.1 |
| sequence | INT | Internal sequence, resets per format\_type.1 |
| priority\_doc\_id | BIGINT | Populated if priority exists natively as an application.1 |
| country | VARCHAR(2) | Jurisdiction of priority filing.1 |
| doc\_number | VARCHAR(20) | Numeric priority identifier.1 |
| date | DATE | Priority filing date.1 |
| linkage\_type | VARCHAR(1) | Defines procedural linkage (e.g., 'A', 'W', 'T').1 |
| is\_active | BOOLEAN | 'Y' indicates active status for family building.1 |

### **4\. Parties Table (Applicants and Inventors)**

Due to identical schemas for applicants and inventors, these entities are consolidated into a highly optimized table utilizing a type flag.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| id | BIGINT | **Primary Key**, Auto-incrementing surrogate. |
| pub\_doc\_id | BIGINT | **Foreign Key** referencing Document Master. |
| party\_type | VARCHAR(10) | 'APPLICANT' or 'INVENTOR'.1 |
| format\_type | VARCHAR(10) | 'docdb', 'docdba', or 'original'.1 |
| sequence | INT | Resets to 1 based on format\_type.1 |
| party\_name | VARCHAR(500) | UTF-8 encoded name block. |
| residence | VARCHAR(2) | Country of residence (mapped in docdb format).1 |
| address\_text | TEXT | Unstructured string address (mapped in docdba format).1 |

### **5\. Designation of States Table**

Captures the jurisdictional scope parsed from the \<exch:designation-of-states\> block.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| id | BIGINT | **Primary Key**, Auto-incrementing surrogate. |
| pub\_doc\_id | BIGINT | **Foreign Key** referencing Document Master.1 |
| treaty\_type | VARCHAR(5) | 'PCT' or 'EPC'.1 |
| designation\_type | VARCHAR(20) | 'regional', 'national', 'contracting', 'validation', etc..1 |
| region\_code | VARCHAR(2) | e.g., 'EP', 'AP', 'EA', 'OA' (for PCT regional).1 |
| country\_code | VARCHAR(2) | Target national state.1 |

### **6\. Patent Classifications Master Table**

A monolithic schema designed to capture the structural diversity of the CPCI, IPC, DOCUS, FI, and FTERM methodologies.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| id | BIGINT | **Primary Key**, Auto-incrementing surrogate. |
| pub\_doc\_id | BIGINT | **Foreign Key** referencing Document Master.1 |
| scheme\_name | VARCHAR(10) | 'CPCI', 'IPC', 'IPCR', 'DOCUS', 'FI', 'FTERM'.1 |
| sequence | INT | Master ordering sequence in XML.1 |
| group\_number | INT | Defines CPCI combination set cluster.1 |
| rank\_number | INT | Order of execution within combination set.1 |
| symbol | VARCHAR(50) | Taxonomic classification string (or IPC main/further).1 |
| class\_value | VARCHAR(1) | 'I' (Invention) or 'A' (Additional info) for CPCI.1 |
| symbol\_pos | VARCHAR(1) | 'F' (First) or 'L' (Later) for CPCI.1 |
| generating\_office | VARCHAR(2) | Authority attributing the CPCI classification.1 |

### **7\. Rich Citations Network Master Table**

Preserves the metadata and origin phase of the cited reference.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| citation\_id | BIGINT | **Primary Key**, Auto-incrementing surrogate. |
| pub\_doc\_id | BIGINT | **Foreign Key** referencing Document Master.1 |
| cited\_phase | VARCHAR(5) | Origin phase (SEA, OPP, TPO, PRS, APP).1 |
| sequence | INT | Sequence within the specific phase.1 |
| srep\_office | VARCHAR(2) | The citing search authority.1 |
| citation\_type | VARCHAR(10) | 'PATENT' (patcit) or 'NPL' (nplcit).1 |
| cited\_doc\_id | BIGINT | Targeted patent doc-id (if native patent citation).1 |
| dnum\_type | VARCHAR(20) | Distinguishes 'publication' vs 'application'.1 |
| npl\_type | VARCHAR(5) | Indicates journals (s), books (b), abstracts (c).1 |
| extracted\_xp | VARCHAR(50) | Fast-index reference for NPL abstracts.1 |
| opponent\_name | VARCHAR(255) | Name of adversarial party in TPO/FOP phases.1 |
| citation\_text | TEXT | Encapsulates native string data or NPL text.1 |

### **8\. Citation Passage Mapping Table**

Preserves the one-to-many relationship dictated by the \<rel-passage\> element, tracking how a single citation impacts multiple claims via different categories.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| id | BIGINT | **Primary Key**, Auto-incrementing surrogate. |
| citation\_id | BIGINT | **Foreign Key** referencing Citations Master.1 |
| category | VARCHAR(10) | Relevance tag (e.g., X, Y, A, I, R).1 |
| rel\_claims | VARCHAR(255) | Associated string of patent claims impacted.1 |
| passage\_text | TEXT | Specific granular location in the targeted prior art.1 |

### **9\. Public Availability Dates Table**

Captures the dynamically generated milestones tracking the legal progression of publication.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| id | BIGINT | **Primary Key**, Auto-incrementing surrogate. |
| pub\_doc\_id | BIGINT | **Foreign Key** referencing Document Master.1 |
| availability\_type | VARCHAR(50) | Tag name (e.g., 'unexamined-printed-without-grant').1 |
| availability\_date | DATE | Extracted date.1 |

### **10\. Abstracts and Titles Table**

Normalizes the textual payloads across various languages and sources.

| Column Name | Data Type | Constraints / Description |
| :---- | :---- | :---- |
| id | BIGINT | **Primary Key**, Auto-incrementing surrogate. |
| pub\_doc\_id | BIGINT | **Foreign Key** referencing Document Master.1 |
| text\_type | VARCHAR(10) | 'TITLE' or 'ABSTRACT'.1 |
| lang | VARCHAR(2) | ISO 639 language code (e.g., 'en', 'de', 'ja').1 |
| format\_type | VARCHAR(10) | 'docdba' or 'original'.1 |
| source | VARCHAR(50) | 'national office', 'transcript', 'PAJ', 'EPO'.1 |
| content | TEXT | Raw UTF-8 encoded text string.1 |

## **Conclusion**

The EPO DOCDB XML format, utilizing WIPO ST.36 as its structural foundation, provides a remarkably dense, multi-faceted dataset designed to encapsulate the vast complexities of the global patent landscape.1 The architecture natively accommodates massive variances in international formatting, character encoding, and legal lifecycles through its nested tag structures, flexible string encoding methodologies, and dynamically rendered simple patent family models.1

For software systems and database architects interfacing with this data, the absolute priority resides in honoring the surrogate key architecture and the strict sequence of operational execution. Relying purely on natural keys—such as document publication numbers and publication dates—will inevitably result in catastrophic database fragmentation and data duplication as the EPO conducts its routine, retroactive data correction sweeps.1

By constructing relational schemas predicated entirely on the surrogate doc-id parameters, utilizing strict referential tracking for formats (docdb vs original), enforcing a transaction-safe wipe-and-load mechanism for element-level status="A" updates, and strictly adhering to the chronological ![][image1] execution of the DeleteRekey, CreateDelete, and Amend physical packages, data engineers can construct a resilient ingestion pipeline that maintains total, high-fidelity synchronicity with the EPO’s master documentation databases.1 The resultant local database will not only ensure highly accurate historical document retrieval but will act as a dynamic platform capable of executing semantic analysis across deeply embedded CPCI classification hierarchies and mathematically complex, rich citation networks.1

#### **Works cited**

1. DOCDB \-User-Documentation-vs-2.5.9.pdf

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAYCAYAAAC8/X7cAAACy0lEQVR4Xu2XSahOYRzG/+bMGzJlIRYKKdNCshCSTCvKkAylEGXItLhKJAsWiIWUDCVz2RgikQ0iIkTXxpCNoRASz/P9z+m85znnfN/VPV9K91dP997ned/3vNN533PNWvi/aAedhvpr0AzGQQfUrBf7oVlqlsA2aIuaZTMXOqtmSbSFnkJjNSiiG7QZOgFdgK5Bt6ANUJugXExr6DU0RYOILtBl6D70G/oK9UyV8Fn+ZJ5/hi6mY1tv3peazIeeQ2vNOxbTA7oHXYU6BD6ZAb2AWomvcBu8Mu/kunRUYZT5ILtqAAaa1xuqQcgu6C00WIOICeaNNIh/FNorXh7XofHQL+iNZSdiDrRVvJBGq5IvMe/cZA0C+EA+/I74z6CV4imdzVeQHDd/1tIkrrDPfIBFHIHOq0k441+g2xoI7AQf/D7wukfezMDLYzq0O/p9hHmdB0lc4ZH5C1sEt+BDNQmXnw0u00Dg7LDczcAbHnnVZo6w81ODvy+Z15sY/c1JPJfEuXCVucVT8FR5Z97YAMmUneblwv0+OvKGBV4efDl5GsXwxGK9eEuwc6uSOJcF0E9LHy7Wybwhqtry9TbfZj8s3dl4BfiziH7QFTXBXfO6I83vkCHpOMM88/KpARDuPQYdNQjgC8YyG8XnZ0OtLbTQsvVI3CF2/olkeSyHPqpJtps3FO7REFZkflADS17saRoE8Jgdo6b5ivP+YP3DkuWxybIvfgVuo5fQY/OtEtMe2gF9h9ZY8UXVCK1WM4IrxFNLb94Y7nsOgBdoLQ6ZH8G59IL2QDfMr2zO2knzK7xPUC4Pzt4x8Xibsq1v5h38YPmD5ApygOHEFcEjlP0pnUXmneDndL3g9xlPoKYM9K9h4zyKZ2tQIryjzqhZJiss+wVZFnzZuX0GaVAmPJt5HE7SoAQaoMVq1gPeI6egvho0A/5L2ZQjtoV/wh/Bp43Kfp1e8QAAAABJRU5ErkJggg==>