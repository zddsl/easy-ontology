import { describe, it, expect, beforeEach } from 'vitest';
import {
  useAppStore,
  loadSavedOntologyFromStorage,
  clearSavedOntology,
  loadImportHistory,
  removeImportHistoryEntry,
  clearImportHistory,
  IMPORT_HISTORY_LIMIT
} from './appStore';
import { cosmicCoffeeOntology, sampleBindings } from '../data/ontology';
import type { Ontology } from '../data/ontology';

const testOntology: Ontology = {
  name: 'demo01',
  description: 'Imported test ontology',
  entityTypes: [
    {
      id: 'employee',
      name: 'Employee',
      description: '',
      icon: '👤',
      color: '#0078D4',
      properties: [{ name: 'employeeId', type: 'string', isIdentifier: true }],
      extendsId: undefined,
    },
  ],
  relationships: [],
};

describe('appStore ontology persistence', () => {
  beforeEach(() => {
    // Reset first, then wipe storage, so the snapshot the reset itself may
    // write doesn't leak into the next test
    useAppStore.getState().resetToDefault();
    localStorage.clear();
  });

  it('loadOntology persists the ontology to localStorage', () => {
    useAppStore.getState().loadOntology(testOntology, []);

    const saved = loadSavedOntologyFromStorage();
    expect(saved).not.toBeNull();
    expect(saved!.ontology.name).toBe('demo01');
    expect(saved!.ontology.entityTypes[0].properties[0].isIdentifier).toBe(true);
    expect(saved!.bindings).toEqual([]);
  });

  it('loadSavedOntologyFromStorage returns null when nothing is saved', () => {
    expect(loadSavedOntologyFromStorage()).toBeNull();
  });

  it('loadSavedOntologyFromStorage tolerates corrupt data', () => {
    localStorage.setItem('ontology-playground:saved-ontology', '{not json');
    expect(loadSavedOntologyFromStorage()).toBeNull();

    localStorage.setItem('ontology-playground:saved-ontology', JSON.stringify({ nope: 1 }));
    expect(loadSavedOntologyFromStorage()).toBeNull();
  });

  it('resetToDefault clears the saved ontology and restores Fourth Coffee', () => {
    useAppStore.getState().loadOntology(testOntology, []);
    expect(loadSavedOntologyFromStorage()).not.toBeNull();

    useAppStore.getState().resetToDefault();
    expect(loadSavedOntologyFromStorage()).toBeNull();
    expect(useAppStore.getState().currentOntology.name).toBe(cosmicCoffeeOntology.name);
    expect(useAppStore.getState().dataBindings).toEqual(sampleBindings);
  });

  it('round-trips a loaded ontology through storage', () => {
    useAppStore.getState().loadOntology(testOntology, []);
    const saved = loadSavedOntologyFromStorage()!;
    // Simulate a fresh page load: restore into the store
    useAppStore.getState().loadOntology(saved.ontology, saved.bindings);
    const restored = useAppStore.getState().currentOntology;
    expect(restored).toEqual(testOntology);
  });
});

describe('import history', () => {
  const ontologyA: Ontology = { ...testOntology, name: 'alpha' };
  const ontologyB: Ontology = { ...testOntology, name: 'beta' };

  beforeEach(() => {
    useAppStore.getState().resetToDefault();
    localStorage.clear();
  });

  it('snapshots the outgoing ontology when a new one is loaded', () => {
    useAppStore.getState().loadOntology(ontologyA, []);
    useAppStore.getState().loadOntology(ontologyB, []);

    const history = loadImportHistory();
    expect(history).toHaveLength(1);
    expect(history[0].ontology.name).toBe('alpha');
    expect(history[0].name).toBe('alpha');
  });

  it('does not snapshot the built-in sample ontology', () => {
    // The store still holds Fourth Coffee — the first import shouldn't record it
    useAppStore.getState().loadOntology(ontologyA, []);
    expect(loadImportHistory()).toHaveLength(0);
  });

  it('does not snapshot a no-op reload of the same ontology', () => {
    useAppStore.getState().loadOntology(ontologyA, []);
    useAppStore.getState().loadOntology(ontologyA, []);
    expect(loadImportHistory()).toHaveLength(0);
  });

  it('snapshots before resetToDefault so a reset is reversible', () => {
    useAppStore.getState().loadOntology(ontologyA, []);
    useAppStore.getState().resetToDefault();

    const history = loadImportHistory();
    expect(history).toHaveLength(1);
    expect(history[0].ontology.name).toBe('alpha');
  });

  it('caps the history length and keeps the most recent entries', () => {
    useAppStore.getState().loadOntology(ontologyA, []);
    for (let i = 0; i < IMPORT_HISTORY_LIMIT + 2; i++) {
      useAppStore.getState().loadOntology({ ...ontologyA, name: `gen-${i}` }, []);
    }

    const history = loadImportHistory();
    expect(history).toHaveLength(IMPORT_HISTORY_LIMIT);
    // The newest snapshot is the ontology replaced by the last import (gen-11 is current).
    // 12 pushes total (alpha + gen-0…gen-10) capped at 10 → the two oldest (alpha, gen-0) are dropped.
    expect(history[0].ontology.name).toBe(`gen-${IMPORT_HISTORY_LIMIT}`);
    expect(history[IMPORT_HISTORY_LIMIT - 1].ontology.name).toBe('gen-1');
  });

  it('removes single entries and clears the whole history', () => {
    useAppStore.getState().loadOntology(ontologyA, []);
    useAppStore.getState().loadOntology(ontologyB, []);
    // History now holds alpha (beta is the current ontology)
    const [entry] = loadImportHistory();
    expect(removeImportHistoryEntry(entry.id)).toHaveLength(0);
    expect(loadImportHistory()).toHaveLength(0);

    // Restoring alpha snapshots beta first
    useAppStore.getState().loadOntology(ontologyA, []);
    expect(loadImportHistory()).toHaveLength(1);
    expect(loadImportHistory()[0].ontology.name).toBe('beta');

    clearImportHistory();
    expect(loadImportHistory()).toHaveLength(0);
  });

  it('names the entry after the imported file and keeps the original load time', () => {
    const importedAt = '2026-08-20T10:00:00.000Z';
    useAppStore.getState().loadOntology(ontologyA, [], { label: 'demo01.rdf', loadedAt: importedAt });
    // Replace it later — the snapshot must still carry the import stamp
    useAppStore.getState().loadOntology(ontologyB, []);

    const [entry] = loadImportHistory();
    expect(entry.name).toBe('demo01.rdf');
    expect(entry.loadedAt).toBe(importedAt);
  });

  it('carries label and load time through the saved-ontology round-trip', () => {
    const importedAt = '2026-08-20T10:00:00.000Z';
    useAppStore.getState().loadOntology(ontologyA, [], { label: 'demo01.rdf', loadedAt: importedAt });

    const saved = loadSavedOntologyFromStorage()!;
    expect(saved.label).toBe('demo01.rdf');
    expect(saved.loadedAt).toBe(importedAt);

    // Simulate a page reload (restore with persisted meta), then replace
    useAppStore.getState().loadOntology(saved.ontology, saved.bindings, { label: saved.label, loadedAt: saved.loadedAt });
    useAppStore.getState().loadOntology(ontologyB, []);
    expect(loadImportHistory()[0].name).toBe('demo01.rdf');
    expect(loadImportHistory()[0].loadedAt).toBe(importedAt);
  });

  it('tolerates corrupt history data', () => {
    localStorage.setItem('ontology-playground:import-history', '{not json');
    expect(loadImportHistory()).toEqual([]);

    localStorage.setItem('ontology-playground:import-history', JSON.stringify([{ nope: 1 }, null]));
    expect(loadImportHistory()).toEqual([]);
  });
});
