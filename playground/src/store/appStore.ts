import { create } from 'zustand';
import type { Quest } from '../data/quests';
import { quests as defaultQuests } from '../data/quests';
import type { Ontology, DataBinding } from '../data/ontology';
import { cosmicCoffeeOntology, sampleBindings } from '../data/ontology';
import { generateQuestsForOntology } from '../data/questGenerator';

export type ThemeId = 'dark' | 'light' | 'aurora' | 'crimson';

export const THEME_OPTIONS: { id: ThemeId; label: string; swatch: string }[] = [
  { id: 'dark', label: 'Dark', swatch: '#1B1B1B' },
  { id: 'light', label: 'Light', swatch: '#F5F5F5' },
  { id: 'aurora', label: 'Aurora', swatch: '#2AAA92' },
  { id: 'crimson', label: 'Crimson', swatch: '#D6002A' },
];

const DARK_BASED_THEMES: ThemeId[] = ['dark', 'aurora'];

/** Whether a theme uses the dark base palette (drives graph/RDF rendering). */
export function isDarkTheme(theme: ThemeId): boolean {
  return DARK_BASED_THEMES.includes(theme);
}

/** CSS class(es) applied to a themed root element. */
export function themeClass(theme: ThemeId): string {
  switch (theme) {
    case 'light':
      return 'light-theme';
    case 'aurora':
      return 'theme-aurora';
    case 'crimson':
      return 'light-theme theme-crimson';
    default:
      return '';
  }
}

function getInitialTheme(): ThemeId {
  if (typeof window === 'undefined' || !('localStorage' in window)) {
    return 'dark';
  }
  try {
    const stored = window.localStorage.getItem('theme');
    if (stored && THEME_OPTIONS.some((t) => t.id === stored)) {
      return stored as ThemeId;
    }
    // Migrate the legacy light/dark flag
    if (window.localStorage.getItem('darkMode') === 'false') {
      return 'light';
    }
    return 'dark';
  } catch {
    return 'dark';
  }
}

const initialTheme = getInitialTheme();

// ─── Ontology persistence ────────────────────────────────────────────────────
// The last ontology loaded into the playground (import, catalogue pick, share
// link) is saved to localStorage and restored on the next visit, so users
// don't have to re-import their RDF every time.

const SAVED_ONTOLOGY_KEY = 'ontology-playground:saved-ontology';

interface SavedOntology {
  ontology: Ontology;
  bindings: DataBinding[];
  /** File name shown in the import history (e.g. "demo01.rdf"), if the ontology came from a file */
  label?: string | null;
  /** When the ontology was originally loaded — preserved across page reloads */
  loadedAt?: string;
}

export function saveOntologyToStorage(
  ontology: Ontology,
  bindings: DataBinding[],
  meta?: { label?: string | null; loadedAt?: string }
): void {
  if (typeof window === 'undefined' || !('localStorage' in window)) return;
  try {
    window.localStorage.setItem(SAVED_ONTOLOGY_KEY, JSON.stringify({
      ontology,
      bindings,
      label: meta?.label ?? null,
      loadedAt: meta?.loadedAt
    } satisfies SavedOntology));
  } catch {
    // Quota exceeded or storage unavailable — persistence is best-effort
  }
}

export function loadSavedOntologyFromStorage(): SavedOntology | null {
  if (typeof window === 'undefined' || !('localStorage' in window)) return null;
  try {
    const raw = window.localStorage.getItem(SAVED_ONTOLOGY_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as SavedOntology;
    if (!parsed?.ontology || !Array.isArray(parsed.ontology.entityTypes)) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function clearSavedOntology(): void {
  if (typeof window === 'undefined' || !('localStorage' in window)) return;
  try {
    window.localStorage.removeItem(SAVED_ONTOLOGY_KEY);
  } catch {
    // noop
  }
}

// ─── Import history ──────────────────────────────────────────────────────────
// Every ontology that is about to be replaced (by an RDF import, a catalogue
// pick, a share link or a reset) is snapshotted here first, so loading a new
// ontology never destroys the previous one. The list is capped and can be
// pruned or restored from the Import/Export modal.

const IMPORT_HISTORY_KEY = 'ontology-playground:import-history';
export const IMPORT_HISTORY_LIMIT = 10;

export interface ImportHistoryEntry {
  id: string;
  /** Display name — the source file name when known, else the ontology name */
  name: string;
  /** When this ontology was originally loaded into the playground */
  loadedAt: string;
  ontology: Ontology;
  bindings: DataBinding[];
}

function isValidHistoryEntry(entry: unknown): entry is ImportHistoryEntry {
  const e = entry as ImportHistoryEntry;
  return Boolean(e?.ontology && Array.isArray(e.ontology.entityTypes));
}

export function loadImportHistory(): ImportHistoryEntry[] {
  if (typeof window === 'undefined' || !('localStorage' in window)) return [];
  try {
    const raw = window.localStorage.getItem(IMPORT_HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isValidHistoryEntry).slice(0, IMPORT_HISTORY_LIMIT);
  } catch {
    return [];
  }
}

function persistImportHistory(entries: ImportHistoryEntry[]): void {
  if (typeof window === 'undefined' || !('localStorage' in window)) return;
  // Drop the oldest entries first if the storage quota is exceeded
  for (let n = entries.length; n > 0; n--) {
    try {
      window.localStorage.setItem(IMPORT_HISTORY_KEY, JSON.stringify(entries.slice(0, n)));
      return;
    } catch {
      // Try again with fewer entries
    }
  }
  try { window.localStorage.removeItem(IMPORT_HISTORY_KEY); } catch { /* noop */ }
}

function pushToImportHistory(ontology: Ontology, bindings: DataBinding[], label: string | null, loadedAt: string): void {
  const entry: ImportHistoryEntry = {
    id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    name: label || ontology.name || 'Untitled ontology',
    loadedAt,
    ontology,
    bindings
  };
  persistImportHistory([entry, ...loadImportHistory()].slice(0, IMPORT_HISTORY_LIMIT));
}

export function removeImportHistoryEntry(id: string): ImportHistoryEntry[] {
  const remaining = loadImportHistory().filter((e) => e.id !== id);
  persistImportHistory(remaining);
  return remaining;
}

export function clearImportHistory(): void {
  if (typeof window === 'undefined' || !('localStorage' in window)) return;
  try { window.localStorage.removeItem(IMPORT_HISTORY_KEY); } catch { /* noop */ }
}

/**
 * Snapshot an outgoing ontology before it gets replaced. Skipped for the
 * built-in sample ontology (restorable via "Reset to Default"). The entry
 * keeps the ontology's original load time and file label.
 */
function snapshotToHistory(ontology: Ontology, bindings: DataBinding[], label: string | null, loadedAt: string): void {
  if (JSON.stringify(ontology) === JSON.stringify(cosmicCoffeeOntology)) return;
  pushToImportHistory(ontology, bindings, label, loadedAt);
}

interface AppState {
  // Ontology State
  currentOntology: Ontology;
  dataBindings: DataBinding[];
  /** Source file name of the current ontology (e.g. "demo01.rdf"), when imported from a file */
  currentOntologyLabel: string | null;
  /** When the current ontology was originally loaded (ISO) */
  currentOntologyLoadedAt: string;
  
  // UI State
  selectedEntityId: string | null;
  selectedRelationshipId: string | null;
  highlightedEntities: string[];
  highlightedRelationships: string[];
  showDataBindings: boolean;
  theme: ThemeId;
  darkMode: boolean;
  
  // Quest State
  availableQuests: Quest[];
  activeQuest: Quest | null;
  currentStepIndex: number;
  completedQuests: string[];
  earnedBadges: { badge: string; icon: string }[];
  totalPoints: number;
  
  // Query State
  queryInput: string;
  queryResult: string | null;
  
  // Ontology Actions
  loadOntology: (ontology: Ontology, bindings?: DataBinding[], meta?: { label?: string | null; loadedAt?: string }) => void;
  resetToDefault: () => void;
  exportOntology: () => string;
  
  // Actions
  selectEntity: (id: string | null) => void;
  selectRelationship: (id: string | null) => void;
  setHighlightedEntities: (ids: string[]) => void;
  setHighlightedRelationships: (ids: string[]) => void;
  setHighlights: (entityIds: string[], relIds: string[]) => void;
  toggleDataBindings: () => void;
  setTheme: (theme: ThemeId) => void;
  toggleDarkMode: () => void;
  
  // Quest Actions
  startQuest: (questId: string) => void;
  advanceQuestStep: () => void;
  completeQuest: () => void;
  abandonQuest: () => void;
  
  // Query Actions
  setQueryInput: (input: string) => void;
  setQueryResult: (result: string | null) => void;
  clearHighlights: () => void;
}

export const useAppStore = create<AppState>((set, get) => ({
  // Initial Ontology State
  currentOntology: cosmicCoffeeOntology,
  dataBindings: sampleBindings,
  currentOntologyLabel: null,
  currentOntologyLoadedAt: new Date().toISOString(),
  
  // Initial UI State
  selectedEntityId: null,
  selectedRelationshipId: null,
  highlightedEntities: [],
  highlightedRelationships: [],
  showDataBindings: false,
  theme: initialTheme,
  darkMode: isDarkTheme(initialTheme),
  
  // Initial Quest State - use default quests for Fourth Coffee
  availableQuests: defaultQuests,
  activeQuest: null,
  currentStepIndex: 0,
  completedQuests: [],
  earnedBadges: [],
  totalPoints: 0,
  
  // Initial Query State
  queryInput: '',
  queryResult: null,
  
  // Ontology Actions
  loadOntology: (ontology, bindings = [], meta) => {
    // Snapshot the outgoing ontology into the import history so it stays
    // recoverable — keeping its original load time and file label. A no-op
    // reload of the same ontology is not recorded.
    const { currentOntology, dataBindings, currentOntologyLabel, currentOntologyLoadedAt } = get();
    if (JSON.stringify(currentOntology) !== JSON.stringify(ontology)) {
      snapshotToHistory(currentOntology, dataBindings, currentOntologyLabel, currentOntologyLoadedAt);
    }
    // Generate new quests based on the loaded ontology
    const newQuests = generateQuestsForOntology(ontology);
    const label = meta?.label ?? null;
    const loadedAt = meta?.loadedAt ?? new Date().toISOString();
    set({
      currentOntology: ontology,
      dataBindings: bindings,
      currentOntologyLabel: label,
      currentOntologyLoadedAt: loadedAt,
      selectedEntityId: null,
      selectedRelationshipId: null,
      highlightedEntities: [],
      highlightedRelationships: [],
      activeQuest: null,
      currentStepIndex: 0,
      availableQuests: newQuests,
      // Reset completed quests when loading a new ontology
      completedQuests: []
    });
    // Persist so the ontology survives a page reload (with its label + load time)
    saveOntologyToStorage(ontology, bindings, { label, loadedAt });
  },

  resetToDefault: () => {
    // Snapshot the outgoing ontology so a reset is reversible too
    const { currentOntology, dataBindings, currentOntologyLabel, currentOntologyLoadedAt } = get();
    snapshotToHistory(currentOntology, dataBindings, currentOntologyLabel, currentOntologyLoadedAt);
    clearSavedOntology();
    set({
      currentOntology: cosmicCoffeeOntology,
      dataBindings: sampleBindings,
      currentOntologyLabel: null,
      currentOntologyLoadedAt: new Date().toISOString(),
      selectedEntityId: null,
      selectedRelationshipId: null,
      highlightedEntities: [],
      highlightedRelationships: [],
      availableQuests: defaultQuests,
      activeQuest: null,
      currentStepIndex: 0,
      completedQuests: []
    });
  },

  exportOntology: () => {
    const { currentOntology, dataBindings } = get();
    return JSON.stringify({ ontology: currentOntology, bindings: dataBindings }, null, 2);
  },
  
  // UI Actions
  selectEntity: (id) => set({ 
    selectedEntityId: id, 
    selectedRelationshipId: null 
  }),
  
  selectRelationship: (id) => set({ 
    selectedRelationshipId: id, 
    selectedEntityId: null 
  }),
  
  setHighlightedEntities: (ids) => set({ highlightedEntities: ids }),
  setHighlightedRelationships: (ids) => set({ highlightedRelationships: ids }),
  setHighlights: (entityIds, relIds) => set({ highlightedEntities: entityIds, highlightedRelationships: relIds }),
  
  toggleDataBindings: () => set((state) => ({ showDataBindings: !state.showDataBindings })),
  setTheme: (theme) => {
    try {
      localStorage.setItem('theme', theme);
    } catch {
      // Ignore persistence errors; still update in-memory state
    }
    set({ theme, darkMode: isDarkTheme(theme) });
  },
  toggleDarkMode: () => {
    const next: ThemeId = isDarkTheme(get().theme) ? 'light' : 'dark';
    get().setTheme(next);
  },
  
  // Quest Actions
  startQuest: (questId) => {
    const { availableQuests } = get();
    const quest = availableQuests.find(q => q.id === questId);
    if (quest) {
      set({ 
        activeQuest: quest, 
        currentStepIndex: 0,
        highlightedEntities: [],
        highlightedRelationships: [],
        selectedEntityId: null,
        selectedRelationshipId: null
      });
    }
  },
  
  advanceQuestStep: () => {
    const { activeQuest, currentStepIndex } = get();
    if (activeQuest && currentStepIndex < activeQuest.steps.length - 1) {
      set({ currentStepIndex: currentStepIndex + 1 });
    } else if (activeQuest) {
      // Last step completed, complete the quest
      get().completeQuest();
    }
  },
  
  completeQuest: () => {
    const { activeQuest, completedQuests, earnedBadges, totalPoints } = get();
    if (activeQuest && !completedQuests.includes(activeQuest.id)) {
      set({
        completedQuests: [...completedQuests, activeQuest.id],
        earnedBadges: [...earnedBadges, { 
          badge: activeQuest.reward.badge, 
          icon: activeQuest.reward.badgeIcon 
        }],
        totalPoints: totalPoints + activeQuest.reward.points,
        activeQuest: null,
        currentStepIndex: 0
      });
    }
  },
  
  abandonQuest: () => set({ 
    activeQuest: null, 
    currentStepIndex: 0,
    highlightedEntities: [],
    highlightedRelationships: []
  }),
  
  // Query Actions
  setQueryInput: (input) => set({ queryInput: input }),
  setQueryResult: (result) => set({ queryResult: result }),
  clearHighlights: () => set({ highlightedEntities: [], highlightedRelationships: [] })
}));
