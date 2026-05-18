# Single-Table Postgres Design

## Recommendation

Use one bibliographic table called `patent_documents`, partitioned by `country`, and keep only the small pipeline state tables separate:

- `delivery_files`
- `ingestion_checkpoints`

That gives you one main data table for patents, while avoiding forcing file-processing state into the same row model.

## Why this fits the current code

The parser already builds a single `ExchangeDocument` object with:

- one publication root
- one application block
- list of parties
- list of priorities
- list of classifications
- list of citations
- list of titles/abstracts
- list of designations
- list of availability dates

That object shape already exists in [models.py](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/models.py:1). The current split happens later in [database.py](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/database.py:48) during insert time.

## Suggested row shape

Keep high-selectivity fields as normal columns:

- `country`
- `pub_doc_id`
- `doc_number`
- `kind_code`
- `date_publ`
- `family_id`
- `is_grant`
- `app_doc_id`
- `app_country`
- `app_number`
- `app_date`

Store nested / repeated structures in JSONB:

- `parties`
- `priorities`
- `classifications`
- `citations`
- `texts`
- `designations`
- `availability_dates`
- `app_extra_data`
- `pub_extra_data`

This is the sweet spot: important filters stay cheap, but the repeating child entities stop exploding into many tables.

## JSONB shapes

### `parties`

Use one object instead of one big flat array:

```json
{
  "applicants": [
    {
      "name": "ACME LTD",
      "format": "docdb",
      "sequence": 1,
      "residence": "GB",
      "address_text": null
    }
  ],
  "inventors": [
    {
      "name": "Jane Smith",
      "format": "docdb",
      "sequence": 1,
      "residence": "GB",
      "address_text": null
    }
  ],
  "others": []
}
```

This is easier to query than a mixed array with `party_type` on every element.

### `priorities`

```json
[
  {
    "format": "docdb",
    "sequence": 1,
    "priority_doc_id": "GB20221111A",
    "country": "GB",
    "doc_number": "20221111",
    "date": "2022-06-01",
    "linkage_type": "P",
    "is_active": true
  }
]
```

### `classifications`

```json
[
  {
    "scheme_name": "CPC",
    "sequence": 1,
    "symbol": "A61K 31/00",
    "class_value": "I",
    "symbol_pos": "F",
    "generating_office": "EP"
  }
]
```

### `citations`

Keep passages nested inside each citation, because they are not useful as a separate table unless you are doing very citation-heavy analytics:

```json
[
  {
    "cited_phase": "SEA",
    "sequence": 1,
    "citation_type": "PATENT",
    "cited_doc_id": "US1234567A",
    "citation_text": null,
    "passages": [
      {
        "category": "X",
        "rel_claims": "1-3",
        "passage_text": "Paragraph 12"
      }
    ]
  }
]
```

### `texts`

Use one JSONB array for titles and abstracts together:

```json
[
  {
    "text_type": "TITLE",
    "lang": "en",
    "format_type": "docdba",
    "source": "invention-title",
    "content": "Example title"
  },
  {
    "text_type": "ABSTRACT",
    "lang": "en",
    "format_type": "docdba",
    "source": "abstract",
    "content": "Example abstract"
  }
]
```

## Partitioning note

Because the table is partitioned by `country`, the primary key should be:

```sql
PRIMARY KEY (country, pub_doc_id)
```

Do not use `PRIMARY KEY (pub_doc_id)` alone on a list-partitioned table unless the partition key is also included.

The current code uses a much simpler rule:

- dedicated country partitions only for `US`, `EP`, `WO`, `CN`, `JP`, `KR`, `DE`, `GB`, and `FR`
- one `DEFAULT` partition for every other country

So this is **not** one partition per country.

## How partitioning works in code

This is not just a schema idea. The current Python code actively creates and uses the partitions.

### 1. Top-level partitioning

The main table is created in [database.py](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/database.py:52) as:

```sql
CREATE TABLE patent_documents (...) PARTITION BY LIST (country)
```

That means every row is first routed by `country`.

Examples:

- `country = 'US'` goes to the `US` country partition
- `country = 'EP'` goes to the `EP` country partition
- `country = 'AM'` goes to the `AM` country partition if it exists
- unknown or not-yet-created country codes fall into `patent_documents_default`

### 2. Dedicated country partitions

The code defines these countries as dedicated partitions:

- `US`
- `EP`
- `WO`
- `CN`
- `JP`
- `KR`
- `DE`
- `GB`
- `FR`

Those are configured in [database.py](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/database.py:15).

For them, `database.py` creates one simple list partition each.

That logic lives in [`_ensure_country_partition()`](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/database.py:219).

Conceptually:

```text
patent_documents
  -> patent_documents_us
  -> patent_documents_ep
  -> patent_documents_wo
  -> patent_documents_cn
  -> patent_documents_jp
  -> patent_documents_kr
  -> patent_documents_de
  -> patent_documents_gb
  -> patent_documents_fr
  -> patent_documents_default
```

Why this matters:

- the highest-interest countries stay isolated in their own physical tables
- every other country goes into one shared default table
- partition management stays very simple

### 3. When partitions are created

There are two moments when partitions are created:

1. During startup in [`init_schema()`](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/database.py:52), the 9 dedicated country partitions are created.
2. During ingestion in [`bulk_upsert_safe()`](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/database.py:423), inserts go to the parent table and Postgres routes rows either to one of those 9 partitions or to `patent_documents_default`.

So yes, the partitioning strategy is reflected in runtime behavior, not just in SQL docs.

### 4. How inserts reach the right partition

The code does **not** insert directly into child tables like `patent_documents_us_h03`.

Instead it always inserts into the parent table:

```sql
INSERT INTO patent_documents (...)
```

That happens in [`bulk_upsert_safe()`](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/database.py:462).

Postgres then routes the row automatically:

1. It reads `country`
2. If the country is one of the 9 dedicated values, it goes to that country's partition
3. Otherwise it goes to `patent_documents_default`

So the code is partition-aware because it supplies the partition key column (`country`) on every insert, and the dedicated partitions already exist.

But the database engine does the final routing.

### 5. What data shape gets inserted

Each `ExchangeDocument` becomes one row in `patent_documents`.

The conversion happens in [`_build_document_row()`](/home/ygpalta/repos/bdds/docdb_ingestion/docdb_ingestion/database.py:400):

- scalar metadata goes into normal columns
- nested repeated sections go into JSONB

Examples:

- `doc.pub_master.country` -> `country`
- `doc.pub_master.pub_doc_id` -> `pub_doc_id`
- `doc.app_master.app_number` -> `app_number`
- `doc.parties` -> `parties` JSONB
- `doc.priorities` -> `priorities` JSONB
- `doc.classifications` -> `classifications` JSONB
- `doc.citations` -> `citations` JSONB

This means the partition key is always present as a real column, not hidden inside JSONB.

## How queries interact with partitions

Partitioning only helps query performance when the query gives Postgres enough information to prune partitions.

### Queries that prune well

Queries with a direct filter on one of the dedicated countries prune well.

Example from [query_biblio.py](/home/ygpalta/repos/bdds/docdb_ingestion/query_biblio.py:36):

```sql
SELECT *
FROM patent_documents
WHERE country = %s AND doc_number = %s AND kind_code = %s
```

This is good partition-aware querying because:

- `country = %s` tells Postgres which list partition to use
- if that country is one of the 9 dedicated ones, only that partition is searched
- if the country is not one of the 9 dedicated ones, Postgres will search the `DEFAULT` partition

So yes, this query pattern is reflected in the partition strategy and benefits from it.

### Queries that do not prune well

Queries that do not filter on `country` cannot use top-level partition pruning effectively.

Examples:

- inventor-name search across all countries
- classification search across all countries
- `app_doc_id` lookups without a country filter
- sibling publication lookup by `app_doc_id`

For example, [query_biblio.py](/home/ygpalta/repos/bdds/docdb_ingestion/query_biblio.py:98) fetches related publications using:

```sql
SELECT country, pub_doc_id, doc_number, kind_code, date_publ, is_grant
FROM patent_documents
WHERE app_doc_id = %s AND pub_doc_id != %s
```

This works correctly, but it is **not strongly partition-pruned**, because `app_doc_id` is not the partition key.

So the honest answer is:

- inserts fully respect partitioning
- country-based lookups benefit strongly
- cross-country lookups still work, but may scan many partitions

## Practical rule for application code

If you want partitioning to help, try to include `country` in the query whenever possible.

Good:

```sql
WHERE country = 'US' AND doc_number = '1234567'
```

Less good:

```sql
WHERE app_doc_id = 'US2023123456A'
```

Still valid, but less partition-friendly.

## Are indexes reflected per partition?

Yes. The indexes defined on `patent_documents` are partitioned indexes, and Postgres creates corresponding child indexes on the dedicated partitions and on the default partition.

That means:

- the B-tree lookup indexes exist on each partition
- the JSONB GIN indexes also exist on each partition

This is useful, and it also keeps index maintenance simpler because there are only 10 physical partitions in total:

- 9 dedicated country partitions
- 1 default partition

## Summary

The partition design is active in the codebase today:

- `init_schema()` creates the partitioned table plus 9 dedicated country partitions
- `bulk_upsert_safe()` inserts into the parent table and Postgres routes each row
- rows for `US`, `EP`, `WO`, `CN`, `JP`, `KR`, `DE`, `GB`, and `FR` go to their own partitions
- rows for all other countries go to `patent_documents_default`
- queries with `country = ...` benefit most when that country has its own dedicated partition
- queries without `country` still work, but do not get the full benefit of partitioning

So the answer is: yes, partitioning is reflected in both inserts and queries, but query performance benefit depends on whether the query uses the partition key.

## Query examples

Find a publication directly:

```sql
SELECT *
FROM patent_documents
WHERE country = 'EP'
  AND doc_number = '1234567'
  AND kind_code = 'A1';
```

Find documents where inventor name matches `smith`:

```sql
SELECT country, pub_doc_id
FROM patent_documents
WHERE jsonb_path_exists(
    parties,
    '$.inventors[*] ? (@.name like_regex "smith" flag "i")'
);
```

Find documents containing CPC symbols starting with `A61K`:

```sql
SELECT country, pub_doc_id
FROM patent_documents
WHERE jsonb_path_exists(
    classifications,
    '$[*] ? (@.scheme_name == "CPC" && @.symbol like_regex "^A61K")'
);
```

Find documents with a priority in `US`:

```sql
SELECT country, pub_doc_id
FROM patent_documents
WHERE jsonb_path_exists(
    priorities,
    '$[*] ? (@.country == "US")'
);
```

## Migration approach from the current schema

The clean migration path is:

1. Create `patent_documents`.
2. Aggregate child tables into JSONB arrays/objects by `pub_doc_id`.
3. Insert into the new table.
4. Switch ingestion writes to target the new table directly.
5. Retire the old child tables after validation.

You already have most of the aggregation logic in [build_patentmaster_local.sql](/home/ygpalta/repos/bdds/docdb_ingestion/build_patentmaster_local.sql:1), so this is more of a consolidation step than a redesign from scratch.

## Practical insertion strategy in Python

Inside the current pipeline, you would stop doing multiple inserts per document and instead build one row dict:

```python
row = {
    "country": doc.pub_master.country,
    "pub_doc_id": doc.pub_master.pub_doc_id,
    "doc_number": doc.pub_master.doc_number,
    "kind_code": doc.pub_master.kind_code,
    "app_doc_id": doc.app_master.app_doc_id,
    "app_country": doc.app_master.app_country,
    "app_number": doc.app_master.app_number,
    "app_date": doc.app_master.app_date,
    "app_extra_data": doc.app_master.extra_data or {},
    "pub_extra_data": doc.pub_master.extra_data or {},
    "parties": {
        "applicants": [...],
        "inventors": [...],
        "others": [...],
    },
    "priorities": [...],
    "classifications": [...],
    "citations": [...],
    "texts": [...],
    "designations": [...],
    "availability_dates": [...],
}
```

Then write it with one `INSERT ... ON CONFLICT ... DO UPDATE`.

## My recommendation

If you want the simplest maintainable version, this should be the target:

- one main table: `patent_documents`
- partition key: `country`
- scalar fields for document/application identity and dates
- JSONB for all repeated nested structures
- GIN indexes only on the JSONB fields you truly query

That gives you a much simpler mental model without turning every patent into one opaque blob.
