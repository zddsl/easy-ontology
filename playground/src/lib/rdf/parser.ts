import type {
  Ontology,
  EntityType,
  Property,
  Relationship,
  RelationshipAttribute,
  DataBinding,
} from '../../data/ontology';

export class RDFParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RDFParseError';
  }
}

/**
 * Get the text content of a child element by local name within a parent element.
 * Searches across common RDF/OWL namespaces.
 */
function getChildText(
  parent: Element,
  localName: string,
  namespace?: string,
): string | null {
  // Try namespace-aware lookup first
  if (namespace) {
    const els = parent.getElementsByTagNameNS(namespace, localName);
    if (els.length > 0) return els[0].textContent;
  }

  // Fallback: try all children by local name match
  for (let i = 0; i < parent.children.length; i++) {
    const child = parent.children[i];
    const childLocal = child.localName || child.tagName.split(':').pop();
    if (childLocal === localName) {
      return child.textContent;
    }
  }
  return null;
}

/**
 * Get the rdf:resource attribute from a child element.
 */
function getChildResource(
  parent: Element,
  localName: string,
): string | null {
  for (let i = 0; i < parent.children.length; i++) {
    const child = parent.children[i];
    const childLocal = child.localName || child.tagName.split(':').pop();
    if (childLocal === localName) {
      return (
        child.getAttribute('rdf:resource') ||
        child.getAttributeNS('http://www.w3.org/1999/02/22-rdf-syntax-ns#', 'resource')
      );
    }
  }
  return null;
}

/**
 * Get all text values from children with a given local name.
 */
function getChildTexts(parent: Element, localName: string): string[] {
  const results: string[] = [];
  for (let i = 0; i < parent.children.length; i++) {
    const child = parent.children[i];
    const childLocal = child.localName || child.tagName.split(':').pop();
    if (childLocal === localName && child.textContent) {
      results.push(child.textContent);
    }
  }
  return results;
}

/**
 * Extract the local name (fragment) from a URI.
 * e.g., "http://example.org/ontology/foo/Customer" → "Customer"
 */
function localNameFromUri(uri: string): string {
  const hashIdx = uri.lastIndexOf('#');
  if (hashIdx >= 0) return uri.substring(hashIdx + 1);
  const slashIdx = uri.lastIndexOf('/');
  if (slashIdx >= 0) return uri.substring(slashIdx + 1);
  return uri;
}

/**
 * Uncapitalize the first character.
 */
function uncapitalize(str: string): string {
  return str.charAt(0).toLowerCase() + str.slice(1);
}

const VALID_PROPERTY_TYPES = ['string', 'integer', 'decimal', 'double', 'date', 'datetime', 'boolean', 'enum'] as const;
type PropertyType = (typeof VALID_PROPERTY_TYPES)[number];

function isValidPropertyType(t: string): t is PropertyType {
  return (VALID_PROPERTY_TYPES as readonly string[]).includes(t);
}

const XSD_TO_TYPE: Record<string, PropertyType> = {
  string: 'string',
  integer: 'integer',
  int: 'integer',
  long: 'integer',
  decimal: 'decimal',
  float: 'decimal',
  double: 'double',
  date: 'date',
  dateTime: 'datetime',
  boolean: 'boolean',
};

const VALID_CARDINALITIES = ['one-to-one', 'one-to-many', 'many-to-one', 'many-to-many'] as const;
type Cardinality = (typeof VALID_CARDINALITIES)[number];

function isValidCardinality(c: string): c is Cardinality {
  return (VALID_CARDINALITIES as readonly string[]).includes(c);
}

const RDF_NS = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#';
const RDFS_NS = 'http://www.w3.org/2000/01/rdf-schema#';
const OWL_NS = 'http://www.w3.org/2002/07/owl#';

/**
 * Heuristic: does a property name look like a unique identifier?
 * Matches "id" plus names ending in "Id", "ID", "_id", "Code", "Number", or "No"
 * (e.g. customerId, customer_id, deptCode, accountNumber, orderNo). Lowercase
 * "id" only matches as the whole name or after an underscore, so ordinary
 * words like "valid" are not misdetected.
 */
const IDENTIFIER_NAME_SUFFIX_RE = /(?:Id|ID|_id|Code|Number|No)$/;

function looksLikeIdentifierName(name: string): boolean {
  const n = name.trim();
  if (/^id$/i.test(n)) return true;
  return n.length > 2 && IDENTIFIER_NAME_SUFFIX_RE.test(n);
}

interface ParsedDatatypeProperty {
  about: string;
  label: string;
  domainUri: string | null;
  rangeUri: string | null;
  comment: string | null;
  isIdentifier: boolean;
  unit: string | null;
  enumValues: string | null;
  propertyType: string | null;
  relationshipAttributeOf: string | null;
  attributeType: string | null;
}

export interface ParseRDFOptions {
  /**
   * Infer identifier properties for standard OWL files that carry no
   * identifier markup: mark ID-like property names (e.g. "customerId",
   * "deptCode") and guarantee every entity ends up with an identifier.
   * Enable for interactive imports (Protégé etc.); leave off for lossless
   * round-trips of Playground-exported RDF.
   * @default false
   */
  inferIdentifiers?: boolean;
}

/**
 * Parse an RDF/XML (OWL) string into an Ontology, optional DataBindings, and
 * non-fatal warnings (e.g. skipped properties, synthesized identifiers).
 */
export function parseRDF(rdfXml: string, options: ParseRDFOptions = {}): {
  ontology: Ontology;
  bindings: DataBinding[];
  warnings: string[];
} {
  const inferIdentifiers = options.inferIdentifiers === true;
  const warnings: string[] = [];
  const parser = new DOMParser();
  const doc = parser.parseFromString(rdfXml, 'application/xml');

  // Check for XML parse errors
  const parseError = doc.querySelector('parsererror');
  if (parseError) {
    throw new RDFParseError(`Malformed XML: ${parseError.textContent?.trim() || 'parse error'}`);
  }

  const root = doc.documentElement;

  // --- Extract ontology metadata ---
  let ontologyName = '';
  let ontologyDescription = '';

  const ontologyEls = root.getElementsByTagNameNS(OWL_NS, 'Ontology');
  if (ontologyEls.length > 0) {
    const ontEl = ontologyEls[0];
    ontologyName = getChildText(ontEl, 'label', RDFS_NS) || '';
    ontologyDescription = getChildText(ontEl, 'comment', RDFS_NS) || '';
  }

  // --- Extract OWL Classes → EntityTypes ---
  const classEls = root.getElementsByTagNameNS(OWL_NS, 'Class');
  const entityMap = new Map<string, EntityType>();
  // about URI → rdfs:subClassOf target URI (resolved once all classes are known)
  const subClassOfUris = new Map<string, string>();

  for (let i = 0; i < classEls.length; i++) {
    const el = classEls[i];
    const about = el.getAttribute('rdf:about') || el.getAttributeNS(RDF_NS, 'about') || '';
    if (!about) continue;

    const className = localNameFromUri(about);
    const entityId = uncapitalize(className);
    const label = getChildText(el, 'label', RDFS_NS) || className;
    const description = getChildText(el, 'comment', RDFS_NS) || '';
    const icon = getChildText(el, 'icon') || '📦';
    const color = getChildText(el, 'color') || '#0078D4';

    const superUri = getChildResource(el, 'subClassOf');
    if (superUri) subClassOfUris.set(about, superUri);

    entityMap.set(about, {
      id: entityId,
      name: label,
      description,
      icon,
      color,
      properties: [],
    });
  }

  // Resolve rdfs:subClassOf targets to entity IDs. Only direct superclass
  // references to known classes become extendsId (restrictions, anonymous
  // classes, and foreign URIs are ignored).
  for (const [about, superUri] of subClassOfUris) {
    const parent = entityMap.get(superUri);
    if (parent) {
      entityMap.get(about)!.extendsId = parent.id;
    }
  }

  // --- Extract DatatypeProperties → Properties + Relationship Attributes ---
  const dtPropEls = root.getElementsByTagNameNS(OWL_NS, 'DatatypeProperty');
  const parsedDtProps: ParsedDatatypeProperty[] = [];

  for (let i = 0; i < dtPropEls.length; i++) {
    const el = dtPropEls[i];
    const about = el.getAttribute('rdf:about') || el.getAttributeNS(RDF_NS, 'about') || '';
    if (!about) continue;

    const comments = getChildTexts(el, 'comment');
    const hasIdentifierComment = comments.some(c => /^identifier\s+property$/i.test(c.trim()));
    const descriptionComment = comments.find(c => !/^identifier\s+property$/i.test(c.trim())) ?? null;

    parsedDtProps.push({
      about,
      label: getChildText(el, 'label', RDFS_NS) || localNameFromUri(about),
      domainUri: getChildResource(el, 'domain'),
      rangeUri: getChildResource(el, 'range'),
      comment: descriptionComment,
      isIdentifier: getChildText(el, 'isIdentifier') === 'true' || hasIdentifierComment,
      unit: getChildText(el, 'unit'),
      enumValues: getChildText(el, 'enumValues'),
      propertyType: getChildText(el, 'propertyType'),
      relationshipAttributeOf: getChildText(el, 'relationshipAttributeOf'),
      attributeType: getChildText(el, 'attributeType'),
    });
  }

  // Collect relationship attributes separately
  const relAttrMap = new Map<string, RelationshipAttribute[]>();

  for (const dtProp of parsedDtProps) {
    if (dtProp.relationshipAttributeOf) {
      const relId = dtProp.relationshipAttributeOf;
      if (!relAttrMap.has(relId)) {
        relAttrMap.set(relId, []);
      }
      relAttrMap.get(relId)!.push({
        name: dtProp.label,
        type: dtProp.attributeType || 'string',
      });
      continue;
    }

    // Regular entity property — match to entity by domain URI
    if (!dtProp.domainUri) {
      warnings.push(
        `Property "${dtProp.label}" has no rdfs:domain and was skipped. Set its domain (in Protégé or your OWL editor) to attach it to a class.`,
      );
      continue;
    }

    const entity = entityMap.get(dtProp.domainUri);
    if (!entity) continue;

    // Determine property type
    let propType: PropertyType = 'string';
    if (dtProp.propertyType && isValidPropertyType(dtProp.propertyType)) {
      propType = dtProp.propertyType;
    } else if (dtProp.rangeUri) {
      const xsdLocal = localNameFromUri(dtProp.rangeUri);
      if (XSD_TO_TYPE[xsdLocal]) {
        propType = XSD_TO_TYPE[xsdLocal];
      }
    }

    const prop: Property = {
      name: dtProp.label,
      type: propType,
    };

    if (dtProp.isIdentifier) {
      prop.isIdentifier = true;
    } else if (
      inferIdentifiers &&
      looksLikeIdentifierName(dtProp.label) &&
      (propType === 'string' || propType === 'integer')
    ) {
      // Standard OWL files (e.g. from Protégé) carry no identifier markup;
      // infer it from common ID naming conventions. Restrict to string/integer
      // since the designer requires identifier properties to be one of those.
      prop.isIdentifier = true;
    }
    if (dtProp.unit) prop.unit = dtProp.unit;
    if (dtProp.enumValues) {
      prop.values = dtProp.enumValues.split(',');
    }
    if (dtProp.comment) prop.description = dtProp.comment;

    entity.properties.push(prop);
  }

  // --- Extract ObjectProperties → Relationships ---
  const objPropEls = root.getElementsByTagNameNS(OWL_NS, 'ObjectProperty');
  const relationships: Relationship[] = [];

  for (let i = 0; i < objPropEls.length; i++) {
    const el = objPropEls[i];
    const about = el.getAttribute('rdf:about') || el.getAttributeNS(RDF_NS, 'about') || '';
    if (!about) continue;

    const relId = localNameFromUri(about);
    const label = getChildText(el, 'label', RDFS_NS) || relId;
    const description = getChildText(el, 'comment', RDFS_NS) || undefined;

    // Get from/to entity IDs — prefer explicit ont:fromEntityId/toEntityId,
    // fallback to domain/range URI.  Always uncapitalize to match entity IDs.
    let fromId = uncapitalize(getChildText(el, 'fromEntityId') || '');
    let toId = uncapitalize(getChildText(el, 'toEntityId') || '');

    if (!fromId) {
      const domainUri = getChildResource(el, 'domain');
      if (domainUri) fromId = uncapitalize(localNameFromUri(domainUri));
    }
    if (!toId) {
      const rangeUri = getChildResource(el, 'range');
      if (rangeUri) toId = uncapitalize(localNameFromUri(rangeUri));
    }

    const cardinalityStr = getChildText(el, 'cardinality') || 'one-to-many';
    const cardinality: Cardinality = isValidCardinality(cardinalityStr)
      ? cardinalityStr
      : 'one-to-many';

    const rel: Relationship = {
      id: relId,
      name: label,
      from: fromId,
      to: toId,
      cardinality,
    };

    if (description) rel.description = description;

    // Attach relationship attributes
    const attrs = relAttrMap.get(relId);
    if (attrs && attrs.length > 0) {
      rel.attributes = attrs;
    }

    // Skip relationships with unresolved source or target
    if (!rel.from || !rel.to) continue;

    relationships.push(rel);
  }

  // --- Extract DataBindings ---
  const bindings: DataBinding[] = [];
  // Look for ont:DataBinding elements (they use the ontology namespace)
  const allElements = root.getElementsByTagName('*');
  for (let i = 0; i < allElements.length; i++) {
    const el = allElements[i];
    const localName = el.localName || el.tagName.split(':').pop();
    if (localName !== 'DataBinding') continue;

    const entityId = getChildText(el, 'boundEntityId') || '';
    const source = getChildText(el, 'source') || '';
    const table = getChildText(el, 'table') || '';
    const mappingTexts = getChildTexts(el, 'columnMapping');

    const columnMappings: Record<string, string> = {};
    for (const mapping of mappingTexts) {
      const eqIdx = mapping.indexOf('=');
      if (eqIdx > 0) {
        columnMappings[mapping.substring(0, eqIdx)] = mapping.substring(eqIdx + 1);
      }
    }

    if (entityId) {
      bindings.push({ entityTypeId: entityId, source, table, columnMappings });
    }
  }

  // --- Build the Ontology ---
  const entityTypes = Array.from(entityMap.values());

  if (!ontologyName && entityTypes.length === 0) {
    throw new RDFParseError('No ontology metadata or OWL classes found in the RDF document.');
  }

  // Ensure every entity has an identifier property — the designer's validation
  // requires one. Prefer an existing string/integer property; otherwise
  // synthesize an "id" property so imports from standard OWL editors
  // (which have no identifier concept) remain usable.
  if (inferIdentifiers) {
    for (const entity of entityTypes) {
      if (entity.properties.some((p) => p.isIdentifier)) continue;
      const candidate = entity.properties.find((p) => p.type === 'string' || p.type === 'integer');
      if (candidate) {
        candidate.isIdentifier = true;
        warnings.push(
          `Entity "${entity.name}" had no identifier property; marked "${candidate.name}" as its identifier.`,
        );
      } else {
        entity.properties.unshift({ name: 'id', type: 'string', isIdentifier: true });
        warnings.push(
          `Entity "${entity.name}" had no properties; added a placeholder "id" identifier property.`,
        );
      }
    }
  }

  const ontology: Ontology = {
    name: ontologyName || 'Imported Ontology',
    description: ontologyDescription,
    entityTypes,
    relationships,
  };

  return { ontology, bindings, warnings };
}
