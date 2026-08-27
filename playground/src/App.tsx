import { useState, useEffect, useCallback, useMemo, lazy, Suspense } from 'react';
import { AnimatePresence } from 'framer-motion';
import {
  Header,
  OntologyGraph,
  InspectorPanel,
  QueryPlayground,
  SearchFilter,
  WelcomeModal,
  AboutModal,
  HelpModal,
  DataSourcesModal,
  ImportExportModal,
  FabricExportModal,
  GalleryModal,
  OntologySummaryModal,
  OntologyDesigner,
  LearnPage,
  Toast,
  CommandPalette,
  GuidedTour,
  isTourDismissed,
  AppFooter,
  OntologyStatsPanel,
  PathFinderPanel
} from './components';
import type { CommandItem } from './components';
import { useAppStore, themeClass, THEME_OPTIONS, loadSavedOntologyFromStorage } from './store/appStore';
import { useDesignerStore } from './store/designerStore';
import { useRoute } from './hooks/useRoute';
import { navigate } from './lib/router';
import { decodeSharePayload } from './lib/shareCodec';
import { parseRDF } from './lib/rdf/parser';  // easy_ontology fork: ?rdf= URL 加载
import type { Catalogue } from './types/catalogue';
import { Search, MessageSquare, Info, LayoutGrid, PenTool, BookOpen, FileJson, HelpCircle, Database, Palette, FileText } from 'lucide-react';
import './styles/app.css';

const AI_BUILDER_ENABLED = import.meta.env.VITE_ENABLE_AI_BUILDER === 'true';

const NLBuilderModal = AI_BUILDER_ENABLED
  ? lazy(() => import('./components/NLBuilderModal').then(m => ({ default: m.NLBuilderModal })))
  : null;

function App() {
  const route = useRoute();

  // easy_ontology fork: ?rdf= 参数 → 极简 viewer 模式，只显示图 + Inspector
  const isViewerMode = typeof window !== 'undefined'
    && new URLSearchParams(window.location.search).has('rdf');

  const [showWelcome, setShowWelcome] = useState(false);
  const [showTour, setShowTour] = useState(() => !isViewerMode && !isTourDismissed());
  const [showAbout, setShowAbout] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [showDataSources, setShowDataSources] = useState(false);
  const [showImportExport, setShowImportExport] = useState(false);
  const [showNLBuilder, setShowNLBuilder] = useState(false);
  const [showFabricExport, setShowFabricExport] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [toast, setToast] = useState<{ message: string; icon: string } | null>(null);
  const [mobilePanel, setMobilePanel] = useState<'graph' | 'inspector' | 'query'>('graph');
  const [showCommandPalette, setShowCommandPalette] = useState(false);
  const { theme, setTheme, earnedBadges, loadOntology } = useAppStore();

  // easy_ontology fork: 暴露选择动作给 E2E 测试（agent-browser 无法对 canvas 坐标点击）
  useEffect(() => {
    if (!isViewerMode) return;
    (window as any).__eoSelect = (id: string | null) => useAppStore.getState().selectEntity(id);
    (window as any).__eoPathFind = async (from: string, to: string) => {
      const { findShortestPath } = await import('./lib/pathFinder');
      const st = useAppStore.getState();
      const p = findShortestPath(from, to, st.currentOntology.relationships);
      if (p) st.setHighlights(p.map(n => n.entityId), p.filter(n => n.via).map(n => n.via!.rel.id));
      else st.clearHighlights();
      return p ? p.map(n => n.via ? `--${n.via.rel.name}--> ${n.entityId}` : n.entityId).join(' ') : 'NO PATH';
    };
  }, [isViewerMode]);

  // Show toast when a new badge is earned
  useEffect(() => {
    if (earnedBadges.length > 0) {
      const latestBadge = earnedBadges[earnedBadges.length - 1];
      setToast({
        message: `Quest Complete! Earned: ${latestBadge.badge}`,
        icon: latestBadge.icon
      });
      
      const timer = setTimeout(() => setToast(null), 4000);
      return () => clearTimeout(timer);
    }
  }, [earnedBadges]);

  // Restore the last loaded ontology (auto-saved on import) so users don't
  // have to re-import their RDF after a page reload. Skipped when a deep link
  // (#/share or #/catalogue) or an easy_ontology ?rdf= URL will load its own.
  useEffect(() => {
    const rdfUrlParam = new URLSearchParams(window.location.search).get('rdf');
    if (rdfUrlParam || route.page === 'share' || (route.page === 'catalogue' && route.ontologyId)) return;
    const saved = loadSavedOntologyFromStorage();
    if (saved) {
      loadOntology(saved.ontology, saved.bindings, { label: saved.label, loadedAt: saved.loadedAt });
      setToast({ message: `Restored your saved ontology "${saved.ontology.name}"`, icon: '💾' });
      const timer = setTimeout(() => setToast(null), 4000);
      return () => clearTimeout(timer);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // easy_ontology fork: ?rdf=<url>&label=<name> — fetch RDF/XML from our backend
  // and load it as the active ontology (pattern mirrors ImportExportModal).
  useEffect(() => {
    const qs = new URLSearchParams(window.location.search);
    const rdfUrl = qs.get('rdf');
    if (!rdfUrl) return;
    let cancelled = false;
    console.log('[easy-ontology] rdf param detected:', rdfUrl);
    fetch(rdfUrl)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to fetch ontology (${res.status})`);
        return res.text();
      })
      .then((text) => {
        if (cancelled) return;
        console.log('[easy-ontology] fetched', text.length, 'chars');
        const { ontology, bindings, warnings } = parseRDF(text, { inferIdentifiers: true });
        console.log('[easy-ontology] parsed:', ontology.name, 'entities=', ontology.entityTypes.length, 'rels=', ontology.relationships.length, 'warnings=', warnings.length);
        const label = qs.get('label') || 'ontology.rdf';
        if (!ontology.name) ontology.name = label.replace(/\.[^.]+$/, '');
        loadOntology(ontology, bindings, { label });
        console.log('[easy-ontology] loadOntology done');
        if (warnings.length > 0) {
          console.warn('[easy-ontology] parse warnings:', warnings);
          // viewer 模式（iframe 嵌入）不打扰用户，警告只进控制台
          if (!isViewerMode) {
            setToast({ message: `Ontology loaded with ${warnings.length} warning(s)`, icon: '⚠️' });
          }
        }
      })
      .catch((err) => {
        setToast({ message: `本体加载失败：${err instanceof Error ? err.message : String(err)}`, icon: '❌' });
        const timer = setTimeout(() => setToast(null), 5000);
        return () => clearTimeout(timer);
      });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Deep-link: /#/catalogue/<id> — load a specific ontology from the catalogue
  useEffect(() => {
    if (route.page === 'catalogue' && route.ontologyId) {
      const id = route.ontologyId;
      fetch(`${import.meta.env.BASE_URL}catalogue.json`)
        .then((res) => {
          if (!res.ok) throw new Error(`Failed to load catalogue (${res.status})`);
          return res.json() as Promise<Catalogue>;
        })
        .then((data) => {
          const entry = data.entries.find((e) => e.id === id);
          if (entry) {
            loadOntology(entry.ontology, entry.bindings);
            // URL stays at /#/catalogue/<id> so it's shareable
          } else {
            // Unknown ontology id — open gallery so the user can pick
            navigate({ page: 'catalogue' });
          }
        })
        .catch(() => {
          // On error, open gallery
          navigate({ page: 'catalogue' });
        });
    }
  }, [route, loadOntology]);

  // Deep-link: /#/share/<data> — decode an inline-shared ontology
  useEffect(() => {
    if (route.page === 'share' && route.data) {
      decodeSharePayload(route.data)
        .then(({ ontology, bindings }) => {
          loadOntology(ontology, bindings);
        })
        .catch(() => {
          // Corrupt or invalid share link — go home
          navigate({ page: 'home' });
        });
    }
  }, [route, loadOntology]);

  // Show gallery only when at /#/catalogue (no specific ontology ID)
  const showGallery = route.page === 'catalogue' && !route.ontologyId;

  const closeGallery = useCallback(() => {
    navigate({ page: 'home' });
  }, []);

  const openGallery = useCallback(() => {
    navigate({ page: 'catalogue' });
  }, []);

  const openDesigner = useCallback(() => {
    // Load the current playground ontology into the designer
    const { currentOntology } = useAppStore.getState();
    useDesignerStore.getState().loadDraft(currentOntology);
    setShowWelcome(false);
    navigate({ page: 'designer' });
  }, []);

  const openLearn = useCallback(() => navigate({ page: 'learn' }), []);

  const cycleTheme = useCallback(() => {
    const idx = THEME_OPTIONS.findIndex((t) => t.id === theme);
    const next = THEME_OPTIONS[(idx + 1) % THEME_OPTIONS.length];
    setTheme(next.id);
  }, [theme, setTheme]);

  // ── Global keyboard shortcuts ──────────────────────────
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Don't fire when typing in inputs/textareas (except for Cmd+K)
      const tag = (e.target as HTMLElement).tagName;
      const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement).isContentEditable;

      // Cmd+K / Ctrl+K — open command palette
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setShowCommandPalette(prev => !prev);
        return;
      }

      if (isInput) return;

      switch (e.key) {
        case '?':
          e.preventDefault();
          setShowHelp(true);
          break;
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  // ── Command palette items ──────────────────────────────
  const commands = useMemo<CommandItem[]>(() => [
    { id: 'catalogue', label: 'Open Catalogue', icon: <LayoutGrid size={18} />, action: openGallery },
    { id: 'designer', label: 'Open Designer', icon: <PenTool size={18} />, action: openDesigner },
    { id: 'learn', label: 'Open Ontology School', icon: <BookOpen size={18} />, action: openLearn },
    { id: 'import-export', label: 'Import / Export', icon: <FileJson size={18} />, action: () => setShowImportExport(true) },
    { id: 'summary', label: 'View Summary', icon: <FileText size={18} />, action: () => setShowSummary(true) },
    { id: 'about', label: 'About & Trademark Notice', icon: <Info size={18} />, action: () => setShowAbout(true) },
    { id: 'help', label: 'Help', icon: <HelpCircle size={18} />, shortcut: '?', action: () => setShowHelp(true) },
    { id: 'data-sources', label: 'Data Sources', icon: <Database size={18} />, action: () => setShowDataSources(true) },
    { id: 'theme', label: 'Switch Theme', icon: <Palette size={18} />, action: cycleTheme },
  ], [openGallery, openDesigner, openLearn, cycleTheme]);

  // Full-page views
  if (route.page === 'designer') {
    return <OntologyDesigner route={route} />;
  }
  if (route.page === 'learn') {
    return <LearnPage route={route} />;
  }

  return (
    <div className={`app-container ${themeClass(theme)} ${isViewerMode ? 'viewer-mode' : ''}`}>
      {!isViewerMode && (
        <Header
          onAboutClick={() => setShowAbout(true)}
          onHelpClick={() => setShowHelp(true)}
          onDataSourcesClick={() => setShowDataSources(true)}
          onImportExportClick={() => setShowImportExport(true)}
          onGalleryClick={openGallery}
          onDesignerClick={openDesigner}
          onLearnClick={openLearn}
          onNLBuilderClick={AI_BUILDER_ENABLED ? () => setShowNLBuilder(true) : undefined}
          onSummaryClick={() => setShowSummary(true)}
        />
      )}
      <OntologyGraph />
      <div className="right-sidebar">
        {!isViewerMode && <OntologyStatsPanel />}
        <PathFinderPanel />
        {!isViewerMode && <SearchFilter />}
        <InspectorPanel />
        {!isViewerMode && <QueryPlayground />}
      </div>

      {/* Mobile bottom tabs — visible only on small screens via CSS */}
      <div className="mobile-panel-tabs">
        <button className={`mobile-tab ${mobilePanel === 'graph' ? 'active' : ''}`} onClick={() => setMobilePanel('graph')}>
          <Search size={18} /> Graph
        </button>
        <button className={`mobile-tab ${mobilePanel === 'inspector' ? 'active' : ''}`} onClick={() => setMobilePanel('inspector')}>
          <Info size={18} /> Inspector
        </button>
        <button className={`mobile-tab ${mobilePanel === 'query' ? 'active' : ''}`} onClick={() => setMobilePanel('query')}>
          <MessageSquare size={18} /> Query
        </button>
      </div>

      {/* Mobile panel drawer — visible only on small screens when a panel is selected */}
      {mobilePanel !== 'graph' && (
        <div className="mobile-panel-drawer">
          <button className="mobile-panel-close" onClick={() => setMobilePanel('graph')}>✕ Close</button>
          {mobilePanel === 'inspector' && (
            <>
              <SearchFilter />
              <InspectorPanel />
            </>
          )}
          {mobilePanel === 'query' && <QueryPlayground />}
        </div>
      )}

      {showTour && (
        <GuidedTour onComplete={() => { setShowTour(false); }} />
      )}

      <AnimatePresence>
        {showWelcome && !showTour && <WelcomeModal onClose={() => setShowWelcome(false)} />}
      </AnimatePresence>

      <AnimatePresence>
        {showAbout && <AboutModal onClose={() => setShowAbout(false)} />}
      </AnimatePresence>

      <AnimatePresence>
        {showHelp && <HelpModal onClose={() => setShowHelp(false)} />}
      </AnimatePresence>

      <AnimatePresence>
        {showDataSources && <DataSourcesModal onClose={() => setShowDataSources(false)} />}
      </AnimatePresence>

      <AnimatePresence>
        {showImportExport && <ImportExportModal onClose={() => setShowImportExport(false)} onFabricPush={() => { setShowImportExport(false); setShowFabricExport(true); }} />}
      </AnimatePresence>

      <AnimatePresence>
        {showFabricExport && <FabricExportModal onClose={() => setShowFabricExport(false)} />}
      </AnimatePresence>

      <AnimatePresence>
        {showGallery && <GalleryModal onClose={closeGallery} />}
      </AnimatePresence>

      {AI_BUILDER_ENABLED && NLBuilderModal && (
        <AnimatePresence>
          {showNLBuilder && (
            <Suspense fallback={null}>
              <NLBuilderModal onClose={() => setShowNLBuilder(false)} />
            </Suspense>
          )}
        </AnimatePresence>
      )}

      <AnimatePresence>
        {showSummary && <OntologySummaryModal onClose={() => setShowSummary(false)} />}
      </AnimatePresence>

      <AnimatePresence>
        {toast && <Toast message={toast.message} icon={toast.icon} />}
      </AnimatePresence>

      <AnimatePresence>
        <CommandPalette
          open={showCommandPalette}
          onClose={() => setShowCommandPalette(false)}
          commands={commands}
        />
      </AnimatePresence>

      <AppFooter />
    </div>
  );
}

export default App;
