import { describe, it, expect } from 'vitest';
import { parseRDF, RDFParseError } from './parser';

describe('parseRDF', () => {
  const minimalRdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF
    xml:base="http://example.org/ontology/test/"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema#"
    xmlns:ont="http://example.org/ontology/test/">

    <owl:Ontology rdf:about="http://example.org/ontology/test/">
        <rdfs:label>Test Ontology</rdfs:label>
        <rdfs:comment>A test</rdfs:comment>
    </owl:Ontology>

    <owl:Class rdf:about="http://example.org/ontology/test/Customer">
        <rdfs:label>Customer</rdfs:label>
        <rdfs:comment>A customer</rdfs:comment>
        <ont:icon>👤</ont:icon>
        <ont:color>#0078D4</ont:color>
    </owl:Class>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_name">
        <rdfs:label>name</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_id">
        <rdfs:label>id</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:isIdentifier rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</ont:isIdentifier>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

</rdf:RDF>`;

  describe('ontology metadata', () => {
    it('extracts ontology name and description', () => {
      const { ontology } = parseRDF(minimalRdf);
      expect(ontology.name).toBe('Test Ontology');
      expect(ontology.description).toBe('A test');
    });

    it('defaults name to "Imported Ontology" when missing', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Foo">
        <rdfs:label>Foo</rdfs:label>
    </owl:Class>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      expect(ontology.name).toBe('Imported Ontology');
    });
  });

  describe('entity types', () => {
    it('parses OWL classes into entity types', () => {
      const { ontology } = parseRDF(minimalRdf);
      expect(ontology.entityTypes).toHaveLength(1);
      const entity = ontology.entityTypes[0];
      expect(entity.id).toBe('customer');
      expect(entity.name).toBe('Customer');
      expect(entity.description).toBe('A customer');
      expect(entity.icon).toBe('👤');
      expect(entity.color).toBe('#0078D4');
    });

    it('defaults icon and color when not provided', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Thing">
        <rdfs:label>Thing</rdfs:label>
    </owl:Class>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      expect(ontology.entityTypes[0].icon).toBe('📦');
      expect(ontology.entityTypes[0].color).toBe('#0078D4');
    });
  });

  describe('properties', () => {
    it('attaches properties to the correct entity via domain', () => {
      const { ontology } = parseRDF(minimalRdf);
      const customer = ontology.entityTypes[0];
      expect(customer.properties).toHaveLength(2);
      const names = customer.properties.map((p) => p.name);
      expect(names).toContain('name');
      expect(names).toContain('id');
    });

    it('parses identifier flag', () => {
      const { ontology } = parseRDF(minimalRdf);
      const idProp = ontology.entityTypes[0].properties.find((p) => p.name === 'id');
      expect(idProp?.isIdentifier).toBe(true);
    });

    it('parses all property types correctly', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ont="http://example.org/ontology/test/">
    <owl:Class rdf:about="http://example.org/ontology/test/Item">
        <rdfs:label>Item</rdfs:label>
    </owl:Class>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/item_s">
        <rdfs:label>s</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Item"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/item_i">
        <rdfs:label>i</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Item"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#integer"/>
        <ont:propertyType>integer</ont:propertyType>
    </owl:DatatypeProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/item_d">
        <rdfs:label>d</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Item"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#decimal"/>
        <ont:propertyType>decimal</ont:propertyType>
    </owl:DatatypeProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/item_dt">
        <rdfs:label>dt</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Item"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#date"/>
        <ont:propertyType>date</ont:propertyType>
    </owl:DatatypeProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/item_dtt">
        <rdfs:label>dtt</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Item"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#dateTime"/>
        <ont:propertyType>datetime</ont:propertyType>
    </owl:DatatypeProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/item_b">
        <rdfs:label>b</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Item"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#boolean"/>
        <ont:propertyType>boolean</ont:propertyType>
    </owl:DatatypeProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/item_e">
        <rdfs:label>e</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Item"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>enum</ont:propertyType>
        <ont:enumValues>X,Y,Z</ont:enumValues>
    </owl:DatatypeProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      const props = ontology.entityTypes[0].properties;
      expect(props).toHaveLength(7);

      const byName = Object.fromEntries(props.map((p) => [p.name, p]));
      expect(byName['s'].type).toBe('string');
      expect(byName['i'].type).toBe('integer');
      expect(byName['d'].type).toBe('decimal');
      expect(byName['dt'].type).toBe('date');
      expect(byName['dtt'].type).toBe('datetime');
      expect(byName['b'].type).toBe('boolean');
      expect(byName['e'].type).toBe('enum');
      expect(byName['e'].values).toEqual(['X', 'Y', 'Z']);
    });

    it('infers identifier from ID-like property names when inferIdentifiers is on', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Department">
        <rdfs:label>Department</rdfs:label>
    </owl:Class>
    <owl:DatatypeProperty rdf:about="http://example.org/departmentId">
        <rdfs:domain rdf:resource="http://example.org/Department"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
    </owl:DatatypeProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/deptCode">
        <rdfs:domain rdf:resource="http://example.org/Department"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
    </owl:DatatypeProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf, { inferIdentifiers: true });
      const byName = Object.fromEntries(
        ontology.entityTypes[0].properties.map((p) => [p.name, p]),
      );
      expect(byName['departmentId'].isIdentifier).toBe(true);
      expect(byName['deptCode'].isIdentifier).toBe(true);

      // Without the flag (default), parsing stays lossless — no inference.
      const { ontology: raw } = parseRDF(rdf);
      expect(raw.entityTypes[0].properties.every((p) => !p.isIdentifier)).toBe(true);
    });

    it('does not infer identifier for ordinary property names', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Employee">
        <rdfs:label>Employee</rdfs:label>
    </owl:Class>
    <owl:DatatypeProperty rdf:about="http://example.org/hasName">
        <rdfs:domain rdf:resource="http://example.org/Employee"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
    </owl:DatatypeProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/valid">
        <rdfs:domain rdf:resource="http://example.org/Employee"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
    </owl:DatatypeProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf, { inferIdentifiers: true });
      // Neither matches the heuristic, but "hasName" is the first string
      // property so the identifier fallback claims it.
      const byName = Object.fromEntries(
        ontology.entityTypes[0].properties.map((p) => [p.name, p]),
      );
      expect(byName['valid'].isIdentifier).toBeUndefined();
      expect(byName['hasName'].isIdentifier).toBe(true);
    });

    it('does not infer identifier for non string/integer ID-like names', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Meter">
        <rdfs:label>Meter</rdfs:label>
    </owl:Class>
    <owl:DatatypeProperty rdf:about="http://example.org/meterNumber">
        <rdfs:domain rdf:resource="http://example.org/Meter"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#decimal"/>
    </owl:DatatypeProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf, { inferIdentifiers: true });
      // decimal cannot be an identifier, so a placeholder id is synthesized instead
      const props = ontology.entityTypes[0].properties;
      expect(props).toHaveLength(2);
      expect(props[0]).toEqual({ name: 'id', type: 'string', isIdentifier: true });
      expect(props[1].isIdentifier).toBeUndefined();
    });

    it('adds placeholder id property to entities with no properties', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Project">
        <rdfs:label>Project</rdfs:label>
    </owl:Class>
</rdf:RDF>`;
      const { ontology, warnings } = parseRDF(rdf, { inferIdentifiers: true });
      expect(ontology.entityTypes[0].properties).toEqual([
        { name: 'id', type: 'string', isIdentifier: true },
      ]);
      expect(warnings.some((w) => w.includes('"Project"'))).toBe(true);
    });

    it('warns when a datatype property has no rdfs:domain', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Employee">
        <rdfs:label>Employee</rdfs:label>
    </owl:Class>
    <owl:DatatypeProperty rdf:about="http://example.org/hasName">
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
    </owl:DatatypeProperty>
</rdf:RDF>`;
      const { ontology, warnings } = parseRDF(rdf, { inferIdentifiers: true });
      expect(ontology.entityTypes[0].properties.map((p) => p.name)).toEqual(['id']);
      expect(warnings.some((w) => w.includes('"hasName"') && w.includes('rdfs:domain'))).toBe(true);
    });

    it('imports a Protégé-style ontology with every entity getting an identifier', () => {
      // Mimics an unmodified Protégé export: global datatype properties with
      // no domain, classes with no identifier markup.
      const rdf = `<?xml version="1.0"?>
<rdf:RDF xmlns="http://example.org/demo/"
     xml:base="http://example.org/demo/"
     xmlns:owl="http://www.w3.org/2002/07/owl#"
     xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
     xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Ontology rdf:about="http://example.org/demo"/>
    <owl:ObjectProperty rdf:about="http://example.org/demo#workFor">
        <rdfs:domain rdf:resource="http://example.org/demo#Employee"/>
        <rdfs:range rdf:resource="http://example.org/demo#Department"/>
    </owl:ObjectProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/demo#hasName">
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
    </owl:DatatypeProperty>
    <owl:Class rdf:about="http://example.org/demo#Department"/>
    <owl:Class rdf:about="http://example.org/demo#Employee"/>
    <owl:Class rdf:about="http://example.org/demo#Project"/>
</rdf:RDF>`;
      const { ontology, warnings } = parseRDF(rdf, { inferIdentifiers: true });
      expect(ontology.entityTypes).toHaveLength(3);
      for (const entity of ontology.entityTypes) {
        expect(entity.properties.some((p) => p.isIdentifier)).toBe(true);
      }
      expect(warnings.some((w) => w.includes('"hasName"'))).toBe(true);
      expect(ontology.relationships).toHaveLength(1);
    });

    it('detects identifier from rdfs:comment "Identifier property"', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ont="http://example.org/ontology/test/">
    <owl:Class rdf:about="http://example.org/ontology/test/Customer">
        <rdfs:label>Customer</rdfs:label>
    </owl:Class>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_id">
        <rdfs:label>id</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <rdfs:comment>Unique customer identifier</rdfs:comment>
        <rdfs:comment>Identifier property</rdfs:comment>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      const idProp = ontology.entityTypes[0].properties.find((p) => p.name === 'id');
      expect(idProp?.isIdentifier).toBe(true);
      // The description should be the non-identifier comment
      expect(idProp?.description).toBe('Unique customer identifier');
    });

    it('parses unit and description', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ont="http://example.org/ontology/test/">
    <owl:Class rdf:about="http://example.org/ontology/test/Item">
        <rdfs:label>Item</rdfs:label>
    </owl:Class>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/item_price">
        <rdfs:label>price</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Item"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#decimal"/>
        <rdfs:comment>The price of the item</rdfs:comment>
        <ont:propertyType>decimal</ont:propertyType>
        <ont:unit>USD</ont:unit>
    </owl:DatatypeProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      const prop = ontology.entityTypes[0].properties[0];
      expect(prop.unit).toBe('USD');
      expect(prop.description).toBe('The price of the item');
    });
  });

  describe('entity inheritance', () => {
    it('parses rdfs:subClassOf into extendsId', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Employee">
        <rdfs:label>Employee</rdfs:label>
    </owl:Class>
    <owl:Class rdf:about="http://example.org/Manager">
        <rdfs:label>Manager</rdfs:label>
        <rdfs:subClassOf rdf:resource="http://example.org/Employee"/>
    </owl:Class>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      const manager = ontology.entityTypes.find((e) => e.id === 'manager');
      expect(manager?.extendsId).toBe('employee');
      const employee = ontology.entityTypes.find((e) => e.id === 'employee');
      expect(employee?.extendsId).toBeUndefined();
    });

    it('ignores subClassOf references to unknown classes or restrictions', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Class rdf:about="http://example.org/Manager">
        <rdfs:subClassOf rdf:resource="http://example.org/Ghost"/>
        <rdfs:subClassOf>
            <owl:Restriction>
                <owl:onProperty rdf:resource="http://example.org/leads"/>
                <owl:someValuesFrom rdf:resource="http://example.org/Department"/>
            </owl:Restriction>
        </rdfs:subClassOf>
    </owl:Class>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      expect(ontology.entityTypes[0].extendsId).toBeUndefined();
    });
  });

  describe('relationships', () => {
    it('parses ObjectProperties as relationships', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ont="http://example.org/ontology/test/">
    <owl:Class rdf:about="http://example.org/ontology/test/Customer"><rdfs:label>Customer</rdfs:label></owl:Class>
    <owl:Class rdf:about="http://example.org/ontology/test/Order"><rdfs:label>Order</rdfs:label></owl:Class>
    <owl:ObjectProperty rdf:about="http://example.org/ontology/test/customer_places_order">
        <rdfs:label>places</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:comment>Customer places orders</rdfs:comment>
        <ont:cardinality>one-to-many</ont:cardinality>
        <ont:fromEntityId>customer</ont:fromEntityId>
        <ont:toEntityId>order</ont:toEntityId>
    </owl:ObjectProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      expect(ontology.relationships).toHaveLength(1);
      const rel = ontology.relationships[0];
      expect(rel.id).toBe('customer_places_order');
      expect(rel.name).toBe('places');
      expect(rel.from).toBe('customer');
      expect(rel.to).toBe('order');
      expect(rel.cardinality).toBe('one-to-many');
      expect(rel.description).toBe('Customer places orders');
    });

    it('falls back to domain/range for from/to when explicit IDs missing', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ont="http://example.org/ontology/test/">
    <owl:Class rdf:about="http://example.org/ontology/test/Foo"><rdfs:label>Foo</rdfs:label></owl:Class>
    <owl:Class rdf:about="http://example.org/ontology/test/Bar"><rdfs:label>Bar</rdfs:label></owl:Class>
    <owl:ObjectProperty rdf:about="http://example.org/ontology/test/foo_rel">
        <rdfs:label>rel</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Foo"/>
        <rdfs:range rdf:resource="http://example.org/ontology/test/Bar"/>
        <ont:cardinality>many-to-many</ont:cardinality>
    </owl:ObjectProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      expect(ontology.relationships[0].from).toBe('foo');
      expect(ontology.relationships[0].to).toBe('bar');
    });

    it('defaults cardinality to one-to-many for invalid values', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ont="http://example.org/ontology/test/">
    <owl:Class rdf:about="http://example.org/ontology/test/A"><rdfs:label>A</rdfs:label></owl:Class>
    <owl:Class rdf:about="http://example.org/ontology/test/B"><rdfs:label>B</rdfs:label></owl:Class>
    <owl:ObjectProperty rdf:about="http://example.org/ontology/test/r">
        <rdfs:label>r</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/A"/>
        <rdfs:range rdf:resource="http://example.org/ontology/test/B"/>
        <ont:cardinality>invalid</ont:cardinality>
    </owl:ObjectProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      expect(ontology.relationships[0].cardinality).toBe('one-to-many');
    });

    it('parses relationship attributes', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ont="http://example.org/ontology/test/">
    <owl:Class rdf:about="http://example.org/ontology/test/A"><rdfs:label>A</rdfs:label></owl:Class>
    <owl:Class rdf:about="http://example.org/ontology/test/B"><rdfs:label>B</rdfs:label></owl:Class>
    <owl:ObjectProperty rdf:about="http://example.org/ontology/test/a_to_b">
        <rdfs:label>links</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/A"/>
        <rdfs:range rdf:resource="http://example.org/ontology/test/B"/>
        <ont:cardinality>many-to-many</ont:cardinality>
        <ont:fromEntityId>a</ont:fromEntityId>
        <ont:toEntityId>b</ont:toEntityId>
    </owl:ObjectProperty>
    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/a_to_b_quantity">
        <rdfs:label>quantity</rdfs:label>
        <rdfs:comment>Relationship attribute for links</rdfs:comment>
        <ont:relationshipAttributeOf>a_to_b</ont:relationshipAttributeOf>
        <ont:attributeType>integer</ont:attributeType>
    </owl:DatatypeProperty>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      const rel = ontology.relationships[0];
      expect(rel.attributes).toHaveLength(1);
      expect(rel.attributes![0].name).toBe('quantity');
      expect(rel.attributes![0].type).toBe('integer');
    });
  });

  describe('data bindings', () => {
    it('parses DataBinding elements', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:ont="http://example.org/ontology/test/">
    <owl:Ontology rdf:about="http://example.org/ontology/test/">
        <rdfs:label>Test</rdfs:label>
    </owl:Ontology>
    <owl:Class rdf:about="http://example.org/ontology/test/Customer">
        <rdfs:label>Customer</rdfs:label>
    </owl:Class>
    <ont:DataBinding rdf:about="http://example.org/ontology/test/binding_customer">
        <ont:boundClass rdf:resource="http://example.org/ontology/test/Customer"/>
        <ont:boundEntityId>customer</ont:boundEntityId>
        <ont:source>Data Lakehouse</ont:source>
        <ont:table>lakehouse.bronze.customers</ont:table>
        <ont:columnMapping>name=full_name</ont:columnMapping>
        <ont:columnMapping>email=email_address</ont:columnMapping>
    </ont:DataBinding>
</rdf:RDF>`;
      const { bindings } = parseRDF(rdf);
      expect(bindings).toHaveLength(1);
      expect(bindings[0].entityTypeId).toBe('customer');
      expect(bindings[0].source).toBe('Data Lakehouse');
      expect(bindings[0].table).toBe('lakehouse.bronze.customers');
      expect(bindings[0].columnMappings).toEqual({
        name: 'full_name',
        email: 'email_address',
      });
    });
  });

  describe('error handling', () => {
    it('throws RDFParseError on malformed XML', () => {
      expect(() => parseRDF('<this is not xml')).toThrow(RDFParseError);
      expect(() => parseRDF('<this is not xml')).toThrow(/Malformed XML/);
    });

    it('throws RDFParseError when no ontology or classes found', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
</rdf:RDF>`;
      expect(() => parseRDF(rdf)).toThrow(RDFParseError);
      expect(() => parseRDF(rdf)).toThrow(/No ontology metadata or OWL classes/);
    });

    it('handles empty entity types gracefully', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
    <owl:Ontology rdf:about="http://example.org/">
        <rdfs:label>Empty</rdfs:label>
    </owl:Ontology>
</rdf:RDF>`;
      const { ontology } = parseRDF(rdf);
      expect(ontology.name).toBe('Empty');
      expect(ontology.entityTypes).toHaveLength(0);
      expect(ontology.relationships).toHaveLength(0);
    });
  });

  describe('namespace handling', () => {
    it('works with custom namespace prefixes', () => {
      const rdf = `<?xml version="1.0" encoding="UTF-8"?>
<r:RDF xmlns:r="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
       xmlns:s="http://www.w3.org/2000/01/rdf-schema#"
       xmlns:o="http://www.w3.org/2002/07/owl#"
       xmlns:my="http://example.org/custom/">
    <o:Ontology r:about="http://example.org/custom/">
        <s:label>Custom NS</s:label>
    </o:Ontology>
    <o:Class r:about="http://example.org/custom/Widget">
        <s:label>Widget</s:label>
        <s:comment>A widget</s:comment>
    </o:Class>
</r:RDF>`;
      const { ontology } = parseRDF(rdf);
      expect(ontology.name).toBe('Custom NS');
      expect(ontology.entityTypes).toHaveLength(1);
      expect(ontology.entityTypes[0].name).toBe('Widget');
    });
  });
});
