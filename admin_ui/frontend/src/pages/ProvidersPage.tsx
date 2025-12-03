import React, { useState, useEffect } from 'react';
import axios from 'axios';
import yaml from 'js-yaml';
import { Plus, Settings, Trash2, Server, AlertCircle, CheckCircle2, Loader2, RefreshCw, Wand2 } from 'lucide-react';
import { ConfigSection } from '../components/ui/ConfigSection';
import { ConfigCard } from '../components/ui/ConfigCard';
import { Modal } from '../components/ui/Modal';

// Provider Forms
import GenericProviderForm from '../components/config/providers/GenericProviderForm';
import LocalProviderForm from '../components/config/providers/LocalProviderForm';
import OpenAIRealtimeProviderForm from '../components/config/providers/OpenAIRealtimeProviderForm';
import DeepgramProviderForm from '../components/config/providers/DeepgramProviderForm';
import GoogleLiveProviderForm from '../components/config/providers/GoogleLiveProviderForm';
import OpenAIProviderForm from '../components/config/providers/OpenAIProviderForm';
import ElevenLabsProviderForm from '../components/config/providers/ElevenLabsProviderForm';
import { ensureModularKey, getModularCapability, isFullAgentProvider } from '../utils/providerNaming';

const ProvidersPage: React.FC = () => {
    const [config, setConfig] = useState<any>({});
    const [loading, setLoading] = useState(true);
    const [editingProvider, setEditingProvider] = useState<string | null>(null);
    const [providerForm, setProviderForm] = useState<any>({});
    const [isNewProvider, setIsNewProvider] = useState(false);
    const [testingProvider, setTestingProvider] = useState<string | null>(null);
    const [testResults, setTestResults] = useState<{ [key: string]: { success: boolean; message: string } | undefined }>({});

    useEffect(() => {
        fetchConfig();
    }, []);

    const fetchConfig = async () => {
        try {
            const res = await axios.get('/api/config/yaml');
            const parsed = yaml.load(res.data.content) as any;
            setConfig(parsed || {});
        } catch (err) {
            console.error('Failed to load config', err);
        } finally {
            setLoading(false);
        }
    };

    const saveConfig = async (newConfig: any) => {
        try {
            await axios.post('/api/config/yaml', { content: yaml.dump(newConfig) });
            setConfig(newConfig);
        } catch (err) {
            console.error('Failed to save config', err);
            alert('Failed to save configuration');
        }
    };

    const handleEditProvider = (name: string) => {
        setEditingProvider(name);
        const providerData = config.providers?.[name] || {};

        if (!providerData.type) {
            if (isFullAgentProvider(providerData)) {
                providerData.type = 'full';
            } else {
                const lowerName = name.toLowerCase();
                if (lowerName.includes('openai')) providerData.type = 'openai';
                else if (lowerName.includes('deepgram')) providerData.type = 'deepgram';
                else if (lowerName.includes('google') || lowerName.includes('gemini')) providerData.type = 'google_live';
                else if (lowerName.includes('elevenlabs')) providerData.type = 'elevenlabs_conversational';
                else if (lowerName.includes('local')) providerData.type = 'local';
                else providerData.type = 'other';
            }
        }

        setProviderForm({ ...providerData, name });
        setIsNewProvider(false);
    };

    const handleAddProvider = () => {
        setEditingProvider('new');
        setProviderForm({
            name: '',
            type: 'full',
            capabilities: ['stt', 'llm', 'tts'],
            enabled: true,
            base_url: ''
        });
        setIsNewProvider(true);
    };

    const handleCreateStarterProviders = async () => {
        const current = config.providers || {};
        const starters: Record<string, any> = {
            local_stt: { type: 'local', capabilities: ['stt'], enabled: true },
            local_llm: { type: 'local', capabilities: ['llm'], enabled: true },
            local_tts: { type: 'local', capabilities: ['tts'], enabled: true }
        };
        let changed = false;
        const nextProviders = { ...current };
        Object.entries(starters).forEach(([key, value]) => {
            if (!nextProviders[key]) {
                nextProviders[key] = value;
                changed = true;
            }
        });
        if (!changed) {
            alert('Starter providers already exist.');
            return;
        }
        await saveConfig({ ...config, providers: nextProviders });
    };

    const handleDeleteProvider = async (name: string) => {
        const pipelines = config.pipelines || {};
        const inUse = Object.entries(pipelines).filter(([_, p]: [string, any]) => p.stt === name || p.llm === name || p.tts === name);
        if (inUse.length > 0) {
            const pipelineList = inUse.map(([n]) => n).join(', ');
            if (!confirm(`Provider "${name}" is used by pipelines: ${pipelineList}. Deleting may break calls. Continue?`)) return;
        }
        if (!confirm(`Are you sure you want to delete provider "${name}"?`)) return;
        const newProviders = { ...(config.providers || {}) };
        delete newProviders[name];
        await saveConfig({ ...config, providers: newProviders });
    };

    const handleSaveProvider = async () => {
        if (!providerForm.name) {
            alert('Provider name is required.');
            return;
        }

        const isFull = isFullAgentProvider(providerForm);
        let finalName = (providerForm.name || '').toLowerCase();
        let capabilities = providerForm.capabilities || [];

        if (!isFull) {
            const cap = getModularCapability(providerForm);
            if (!cap) {
                alert('Select exactly one capability for modular providers (STT/LLM/TTS).');
                return;
            }
            finalName = ensureModularKey(finalName, cap);
            capabilities = [cap];
        } else {
            capabilities = ['stt', 'llm', 'tts'];
        }

        const providerKey = isNewProvider ? finalName : editingProvider;
        if (!providerKey) return;

        const newConfig = { ...config };
        if (!newConfig.providers) newConfig.providers = {};

        if ((isNewProvider || editingProvider !== finalName) && newConfig.providers[finalName]) {
            alert(`Provider "${finalName}" already exists.`);
            return;
        }

        const existingData = !isNewProvider && editingProvider ? (config.providers?.[editingProvider] || {}) : {};
        const providerData = { ...existingData, ...providerForm, name: finalName, capabilities };

        if (!isFull && providerData.capabilities.length !== 1) {
            alert('Modular providers must have exactly one capability.');
            return;
        }

        if (!isFull && !providerData.capabilities[0]) {
            alert('Capability is required for modular providers.');
            return;
        }

        if (!isFull) {
            const cap = providerData.capabilities[0];
            providerData.name = ensureModularKey(providerData.name, cap);
        }

        if (!isNewProvider && editingProvider && editingProvider !== finalName) {
            delete newConfig.providers[editingProvider];
            if (newConfig.pipelines) {
                Object.entries(newConfig.pipelines).forEach(([pipelineName, pipeline]: [string, any]) => {
                    const updated = { ...pipeline };
                    let changed = false;
                    (['stt', 'llm', 'tts'] as const).forEach(role => {
                        if (updated[role] === editingProvider) {
                            updated[role] = finalName;
                            changed = true;
                        }
                    });
                    if (changed) newConfig.pipelines[pipelineName] = updated;
                });
            }
        }

        newConfig.providers[finalName] = providerData;

        await saveConfig(newConfig);
        setEditingProvider(null);
    };

    const handleTestConnection = async (name: string, providerData: any) => {
        setTestingProvider(name);
        setTestResults(prev => ({ ...prev, [name]: undefined }));
        try {
            const response = await axios.post('/api/config/providers/test', { name, config: providerData });
            setTestResults(prev => ({
                ...prev,
                [name]: { success: response.data.success, message: response.data.message || 'Connection successful!' }
            }));
        } catch (err: any) {
            setTestResults(prev => ({
                ...prev,
                [name]: { success: false, message: err.response?.data?.detail || 'Connection failed' }
            }));
        } finally {
            setTestingProvider(null);
        }
    };

    const renderProviderForm = () => {
        const updateForm = (newValues: any) => setProviderForm({ ...providerForm, ...newValues });

        switch (providerForm.type) {
            case 'local':
                return <LocalProviderForm config={providerForm} onChange={updateForm} />;
            case 'openai_realtime':
                return <OpenAIRealtimeProviderForm config={providerForm} onChange={updateForm} />;
            case 'deepgram':
                return <DeepgramProviderForm config={providerForm} onChange={updateForm} />;
            case 'google_live':
                return <GoogleLiveProviderForm config={providerForm} onChange={updateForm} />;
            case 'openai':
                return <OpenAIProviderForm config={providerForm} onChange={updateForm} />;
            case 'elevenlabs_conversational':
                return <ElevenLabsProviderForm config={providerForm} onChange={updateForm} />;
            default:
                return <GenericProviderForm config={providerForm} onChange={updateForm} isNew={isNewProvider} />;
        }
    };

    if (loading) return <div className="p-8 text-center text-muted-foreground">Loading configuration...</div>;

    return (
        <div className="space-y-6">
            <div className="bg-yellow-500/10 border border-yellow-500/20 text-yellow-500 p-4 rounded-md flex items-center justify-between">
                <div className="flex items-center">
                    <AlertCircle className="w-5 h-5 mr-2" />
                    Changes to provider configurations require a system restart to take effect.
                </div>
                <button
                    onClick={() => window.location.reload()}
                    className="flex items-center text-xs bg-yellow-500/20 hover:bg-yellow-500/30 px-3 py-1.5 rounded transition-colors"
                >
                    <RefreshCw className="w-3 h-3 mr-1.5" />
                    Reload UI
                </button>
            </div>

            <div className="flex justify-between items-center">
                <div>
                    <h1 className="text-3xl font-bold tracking-tight">Providers</h1>
                    <p className="text-muted-foreground mt-1">
                        Manage connections to external AI services (STT, LLM, TTS).
                        <span className="block text-xs mt-1">
                            Modular providers are auto-suffixed (e.g., <code>_stt</code>) to match engine factories. Full agents stay unsuffixed.
                        </span>
                    </p>
                </div>
                <div className="flex gap-2">
                    <button
                        onClick={handleCreateStarterProviders}
                        className="inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground h-9 px-4 py-2"
                    >
                        <Wand2 className="w-4 h-4 mr-2" />
                        Add Starter Providers
                    </button>
                    <button
                        onClick={handleAddProvider}
                        className="inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 bg-primary text-primary-foreground shadow hover:bg-primary/90 h-9 px-4 py-2"
                    >
                        <Plus className="w-4 h-4 mr-2" />
                        Add Provider
                    </button>
                </div>
            </div>

            <ConfigSection title="Full Agents" description="End-to-end agents (STT+LLM+TTS) that bypass pipelines.">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {Object.entries(config.providers || {}).filter(([_, p]) => isFullAgentProvider(p)).map(([name, providerData]: [string, any]) => (
                        <ConfigCard key={name} className="group relative hover:border-primary/50 transition-colors">
                            <div className="flex items-start justify-between">
                                <div className="flex items-center gap-3">
                                    <div className={`p-2 rounded-md ${providerData.enabled ? 'bg-secondary' : 'bg-muted'}`}>
                                        <Server className={`w-5 h-5 ${providerData.enabled ? 'text-primary' : 'text-muted-foreground'}`} />
                                    </div>
                                    <div>
                                        <div className="flex items-center gap-2">
                                            <h4 className={`font-semibold text-lg ${!providerData.enabled && 'text-muted-foreground'}`}>{name}</h4>
                                            {!providerData.enabled && (
                                                <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded">Disabled</span>
                                            )}
                                        </div>
                                        <div className="flex flex-wrap gap-2 mt-1">
                                            {(providerData.model || providerData.voice || providerData.tts_model || providerData.llm_model || providerData.tts_voice_name) && (
                                                <>
                                                    {providerData.model && (
                                                        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 text-foreground">
                                                            {providerData.model}
                                                        </span>
                                                    )}
                                                    {providerData.voice && (
                                                        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 text-muted-foreground">
                                                            {providerData.voice}
                                                        </span>
                                                    )}
                                                    {providerData.tts_model && (
                                                        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 text-muted-foreground">
                                                            {providerData.tts_model}
                                                        </span>
                                                    )}
                                                    {providerData.llm_model && (
                                                        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 text-muted-foreground">
                                                            {providerData.llm_model}
                                                        </span>
                                                    )}
                                                    {providerData.tts_voice_name && (
                                                        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 text-muted-foreground">
                                                            {providerData.tts_voice_name}
                                                        </span>
                                                    )}
                                                </>
                                            )}
                                        </div>
                                    </div>
                                </div>
                                <div className="flex items-center gap-2">
                                    <div className="flex items-center space-x-2 mr-2">
                                        <label className="relative inline-flex items-center cursor-pointer">
                                            <input
                                                type="checkbox"
                                                className="sr-only peer"
                                                checked={providerData.enabled ?? true}
                                                onChange={async (e) => {
                                                    const newProviders = { ...config.providers };
                                                    newProviders[name] = { ...providerData, enabled: e.target.checked };
                                                    await saveConfig({ ...config, providers: newProviders });
                                                }}
                                            />
                                            <div className="w-9 h-5 bg-input peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-ring rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary"></div>
                                        </label>
                                    </div>
                                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                        <button
                                            onClick={() => handleTestConnection(name, providerData)}
                                            disabled={testingProvider === name}
                                            className="p-2 hover:bg-accent rounded-md text-muted-foreground hover:text-foreground disabled:opacity-50"
                                            title="Test Connection"
                                        >
                                            {testingProvider === name ? (
                                                <Loader2 className="w-4 h-4 animate-spin" />
                                            ) : testResults[name]?.success ? (
                                                <CheckCircle2 className="w-4 h-4 text-green-500" />
                                            ) : testResults[name]?.success === false ? (
                                                <AlertCircle className="w-4 h-4 text-destructive" />
                                            ) : (
                                                <Server className="w-4 h-4" />
                                            )}
                                        </button>
                                        <button
                                            onClick={() => handleEditProvider(name)}
                                            className="p-2 hover:bg-accent rounded-md text-muted-foreground hover:text-foreground"
                                        >
                                            <Settings className="w-4 h-4" />
                                        </button>
                                        <button
                                            onClick={() => handleDeleteProvider(name)}
                                            className="p-2 hover:bg-destructive/10 rounded-md text-destructive"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>
                                </div>
                            </div>
                            {testResults[name] && (
                                <div className={`mt-2 p-2 rounded text-xs ${testResults[name]?.success
                                    ? 'bg-green-500/10 text-green-600 dark:text-green-400'
                                    : 'bg-destructive/10 text-destructive'
                                    }`}>
                                    {testResults[name]?.message}
                                </div>
                            )}
                        </ConfigCard>
                    ))}
                    {Object.entries(config.providers || {}).filter(([_, p]) => isFullAgentProvider(p)).length === 0 && (
                        <div className="col-span-full p-8 border border-dashed rounded-lg text-center text-muted-foreground">
                            No full agents configured. Click "Add Provider" to get started.
                        </div>
                    )}
                </div>
            </ConfigSection>

            <ConfigSection title="Modular Providers" description="Providers you can mix in pipelines (STT/LLM/TTS) based on their capabilities.">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {Object.entries(config.providers || {}).filter(([_, p]) => !isFullAgentProvider(p)).map(([name, providerData]: [string, any]) => (
                        <ConfigCard key={name} className="group relative hover:border-primary/50 transition-colors">
                            <div className="flex items-start justify-between">
                                <div className="flex items-center gap-3">
                                    <div className={`p-2 rounded-md ${providerData.enabled ? 'bg-secondary' : 'bg-muted'}`}>
                                        <Server className={`w-5 h-5 ${providerData.enabled ? 'text-primary' : 'text-muted-foreground'}`} />
                                    </div>
                                    <div>
                                        <div className="flex items-center gap-2">
                                            <h4 className={`font-semibold text-lg ${!providerData.enabled && 'text-muted-foreground'}`}>{name}</h4>
                                            {!providerData.enabled && (
                                                <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded">Disabled</span>
                                            )}
                                        </div>
                                        <div className="flex flex-wrap gap-2 mt-1">
                                            {(providerData.capabilities || []).map((cap: string) => (
                                                <span key={cap} className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 text-muted-foreground">
                                                    {cap.toUpperCase()}
                                                </span>
                                            ))}
                                        </div>
                                    </div>
                                </div>
                                <div className="flex items-center gap-2">
                                    <div className="flex items-center space-x-2 mr-2">
                                        <label className="relative inline-flex items-center cursor-pointer">
                                            <input
                                                type="checkbox"
                                                className="sr-only peer"
                                                checked={providerData.enabled ?? true}
                                                onChange={async (e) => {
                                                    const newProviders = { ...config.providers };
                                                    newProviders[name] = { ...providerData, enabled: e.target.checked };
                                                    await saveConfig({ ...config, providers: newProviders });
                                                }}
                                            />
                                            <div className="w-9 h-5 bg-input peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-ring rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary"></div>
                                        </label>
                                    </div>
                                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                        <button
                                            onClick={() => handleTestConnection(name, providerData)}
                                            disabled={testingProvider === name}
                                            className="p-2 hover:bg-accent rounded-md text-muted-foreground hover:text-foreground disabled:opacity-50"
                                            title="Test Connection"
                                        >
                                            {testingProvider === name ? (
                                                <Loader2 className="w-4 h-4 animate-spin" />
                                            ) : testResults[name]?.success ? (
                                                <CheckCircle2 className="w-4 h-4 text-green-500" />
                                            ) : testResults[name]?.success === false ? (
                                                <AlertCircle className="w-4 h-4 text-destructive" />
                                            ) : (
                                                <Server className="w-4 h-4" />
                                            )}
                                        </button>
                                        <button
                                            onClick={() => handleEditProvider(name)}
                                            className="p-2 hover:bg-accent rounded-md text-muted-foreground hover:text-foreground"
                                        >
                                            <Settings className="w-4 h-4" />
                                        </button>
                                        <button
                                            onClick={() => handleDeleteProvider(name)}
                                            className="p-2 hover:bg-destructive/10 rounded-md text-destructive"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>
                                </div>
                            </div>
                            {testResults[name] && (
                                <div className={`mt-2 p-2 rounded text-xs ${testResults[name]?.success
                                    ? 'bg-green-500/10 text-green-600 dark:text-green-400'
                                    : 'bg-destructive/10 text-destructive'
                                    }`}>
                                    {testResults[name]?.message}
                                </div>
                            )}
                        </ConfigCard>
                    ))}
                    {Object.entries(config.providers || {}).filter(([_, p]) => !isFullAgentProvider(p)).length === 0 && (
                        <div className="col-span-full p-8 border border-dashed rounded-lg text-center text-muted-foreground">
                            No composable providers configured. Click "Add Provider" to get started.
                        </div>
                    )}
                </div>
            </ConfigSection>

            <Modal
                isOpen={!!editingProvider}
                onClose={() => setEditingProvider(null)}
                title={isNewProvider ? 'Add Provider' : `Edit Provider: ${editingProvider}`}
                size="lg"
                footer={
                    <div className="flex w-full justify-between items-center">
                        <div className="text-xs text-muted-foreground">
                            Modular providers are automatically suffixed for their capability (e.g., <code>openai_stt</code>, <code>openai_llm</code>, <code>openai_tts</code>).
                        </div>
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => handleTestConnection(providerForm.name || 'new_provider', providerForm)}
                                disabled={!!testingProvider || !providerForm.name}
                                className="inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground h-9 px-4 py-2"
                            >
                                {testingProvider === (providerForm.name || 'new_provider') ? (
                                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                                ) : (
                                    <Server className="w-4 h-4 mr-2" />
                                )}
                                Test Connection
                            </button>
                            {testResults[providerForm.name || 'new_provider'] && (
                                <span className={`text-xs ${testResults[providerForm.name || 'new_provider']?.success ? 'text-green-500' : 'text-destructive'}`}>
                                    {testResults[providerForm.name || 'new_provider']?.success ? 'Success' : 'Failed'}
                                </span>
                            )}
                        </div>
                        <div className="flex gap-2">
                            <button
                                onClick={() => setEditingProvider(null)}
                                className="inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground h-9 px-4 py-2"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={handleSaveProvider}
                                className="inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 bg-primary text-primary-foreground shadow hover:bg-primary/90 h-9 px-4 py-2"
                            >
                                Save Changes
                            </button>
                        </div>
                    </div>
                }
            >
                {renderProviderForm()}
            </Modal>
        </div>
    );
};

export default ProvidersPage;
