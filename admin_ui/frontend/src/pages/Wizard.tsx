import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertCircle, AlertTriangle, ArrowRight, Loader2, Cloud, Server, Shield, Zap, SkipForward, CheckCircle, Terminal, Copy, HardDrive, Play } from 'lucide-react';
import axios from 'axios';

interface SetupConfig {
    provider: string;
    asterisk_host: string;
    asterisk_username: string;
    asterisk_password: string;
    asterisk_port?: number;
    asterisk_scheme?: string;
    asterisk_app?: string;
    openai_key?: string;
    deepgram_key?: string;
    google_key?: string;
    elevenlabs_key?: string;
    cartesia_key?: string;
    greeting: string;
    ai_name: string;
    ai_role: string;
}

const Wizard = () => {
    const navigate = useNavigate();
    const [step, setStep] = useState(1);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [config, setConfig] = useState<SetupConfig>({
        provider: 'openai_realtime',
        asterisk_host: '127.0.0.1',
        asterisk_username: 'asterisk',
        asterisk_password: '',
        asterisk_port: 8088,
        asterisk_scheme: 'http',
        asterisk_app: 'asterisk-ai-voice-agent',
        openai_key: '',
        deepgram_key: '',
        google_key: '',
        greeting: 'Hello, how can I help you today?',
        ai_name: 'Asterisk Agent',
        ai_role: 'Helpful Assistant'
    });

    const [validations, setValidations] = useState({
        openai: false,
        deepgram: false,
        google: false
    });

    const [showSkipConfirm, setShowSkipConfirm] = useState(false);
    const [engineStatus, setEngineStatus] = useState<{
        running: boolean;
        exists: boolean;
        checked: boolean;
    }>({ running: false, exists: false, checked: false });
    const [startingEngine, setStartingEngine] = useState(false);
    
    // Local AI Server state
    const [localAIStatus, setLocalAIStatus] = useState<{
        tier: string;
        tierInfo: any;
        cpuCores: number;
        ramGb: number;
        gpuDetected: boolean;
        modelsReady: boolean;
        existingModels: { stt: string[]; llm: string[]; tts: string[] };
        downloading: boolean;
        downloadOutput: string[];
        downloadCompleted: boolean;
        serverStarted: boolean;
        serverLogs: string[];
        serverReady: boolean;
    }>({
        tier: '',
        tierInfo: {},
        cpuCores: 0,
        ramGb: 0,
        gpuDetected: false,
        modelsReady: false,
        existingModels: { stt: [], llm: [], tts: [] },
        downloading: false,
        downloadOutput: [],
        downloadCompleted: false,
        serverStarted: false,
        serverLogs: [],
        serverReady: false
    });

    const handleSkip = () => {
        setShowSkipConfirm(true);
    };

    const confirmSkip = async () => {
        try {
            await axios.post('/api/wizard/skip');
            navigate('/');
        } catch (err: any) {
            setError('Failed to skip setup: ' + err.message);
            setShowSkipConfirm(false);
        }
    };

    const handleTestConnection = async () => {
        setLoading(true);
        setError(null);
        try {
            await axios.post('/api/wizard/validate-connection', {
                host: config.asterisk_host,
                username: config.asterisk_username,
                password: config.asterisk_password,
                port: config.asterisk_port,
                scheme: config.asterisk_scheme
            });
            alert('Successfully connected to Asterisk!');
        } catch (err: any) {
            setError('Connection failed: ' + (err.response?.data?.detail || err.message));
        } finally {
            setLoading(false);
        }
    };

    const handleTestKey = async (provider: string, key: string) => {
        if (!key) {
            setError(`${provider} API Key is required`);
            return;
        }
        setLoading(true);
        setError(null);
        try {
            const res = await axios.post('/api/wizard/validate-key', {
                provider: provider === 'openai_realtime' ? 'openai' : provider,
                api_key: key
            });
            if (!res.data.valid) throw new Error(`${provider} Key Invalid: ${res.data.error}`);

            setValidations(prev => ({ ...prev, [provider === 'openai_realtime' ? 'openai' : provider]: true }));
            alert(`${provider} API Key is valid!`);
        } catch (err: any) {
            setError(err.message);
            setValidations(prev => ({ ...prev, [provider === 'openai_realtime' ? 'openai' : provider]: false }));
        } finally {
            setLoading(false);
        }
    };

    const verifyLocalAIHealth = async () => {
        try {
            const res = await axios.get('/api/system/health');
            const status = res.data?.local_ai_server?.status;
            if (status !== 'connected') {
                throw new Error('Local AI Server is not reachable. Please start the local-ai-server container and retry.');
            }
        } catch (err: any) {
            throw new Error(err?.message || 'Local AI Server health check failed.');
        }
    };

    const handleNext = async () => {
        setError(null);

        // Basic required-field validation for non-technical users
        if (step === 4) {
            const missing: string[] = [];
            if (!config.asterisk_host) missing.push('Asterisk host');
            if (!config.asterisk_username) missing.push('ARI username');
            if (!config.asterisk_password) missing.push('ARI password');

            if (missing.length) {
                setError(`${missing.join(', ')} ${missing.length === 1 ? 'is' : 'are'} required.`);
                return;
            }

            // Provider key requirement for selected provider
            if (config.provider === 'openai_realtime' && !config.openai_key) {
                setError('OpenAI API key is required for OpenAI Realtime.');
                return;
            }
            if (config.provider === 'deepgram' && !config.deepgram_key) {
                setError('Deepgram API key is required for Deepgram.');
                return;
            }
            if (config.provider === 'google_live' && !config.google_key) {
                setError('Google API key is required for Google Live.');
                return;
            }
        }

        if (step === 3) {
            // Validate keys before proceeding
            setLoading(true);
            try {
                if (config.provider === 'openai_realtime' || config.provider === 'local_hybrid') {
                    if (config.openai_key) {
                        const res = await axios.post('/api/wizard/validate-key', {
                            provider: 'openai',
                            api_key: config.openai_key
                        });
                        if (!res.data.valid) throw new Error(`OpenAI Key Invalid: ${res.data.error}`);
                        setValidations(prev => ({ ...prev, openai: true }));
                    } else if (config.provider === 'openai_realtime') {
                        throw new Error('OpenAI API Key is required for OpenAI Realtime provider');
                    }
                }

                if (config.provider === 'deepgram') {
                    if (config.deepgram_key) {
                        const res = await axios.post('/api/wizard/validate-key', {
                            provider: 'deepgram',
                            api_key: config.deepgram_key
                        });
                        if (!res.data.valid) throw new Error(`Deepgram Key Invalid: ${res.data.error}`);
                        setValidations(prev => ({ ...prev, deepgram: true }));
                    } else {
                        throw new Error('Deepgram API Key is required for Deepgram provider');
                    }
                }

                if (config.provider === 'google_live') {
                    if (config.google_key) {
                        const res = await axios.post('/api/wizard/validate-key', {
                            provider: 'google',
                            api_key: config.google_key
                        });
                        if (!res.data.valid) throw new Error(`Google Key Invalid: ${res.data.error}`);
                        setValidations(prev => ({ ...prev, google: true }));
                    } else {
                        throw new Error('Google API Key is required for Google Live provider');
                    }
                }

                // Only verify Local AI health for local_hybrid on step 3
                // For "local" (Full), server is started in step 5 after model download
                if (config.provider === 'local_hybrid') {
                    await verifyLocalAIHealth();
                }

                setStep(step + 1);
            } catch (err: any) {
                setError(err.message);
            } finally {
                setLoading(false);
            }
        } else if (step === 4) {
            // Validate ARI fields
            if (!config.asterisk_host) {
                setError('Asterisk Host is required');
                return;
            }
            if (!config.asterisk_username) {
                setError('ARI Username is required');
                return;
            }
            if (!config.asterisk_password) {
                setError('ARI Password is required');
                return;
            }

            // Validate secret strength (basic check)
            if (config.asterisk_password.length < 8) {
                setError('ARI Password must be at least 8 characters long');
                return;
            }

            // Health Check for Local Hybrid Provider only
            // Note: For "local" (Full) provider, server is started in step 5
            if (config.provider === 'local_hybrid') {
                setLoading(true);
                try {
                    // Check if Local AI Server is reachable via backend proxy
                    const res = await axios.get('/api/system/health');
                    if (res.data.local_ai_server?.status !== 'connected') {
                        setError(`Local AI Server is not reachable (Status: ${res.data.local_ai_server?.status}). Please ensure it is running.`);
                        setLoading(false);
                        return;
                    }
                } catch (err) {
                    setError('Failed to contact system health endpoint. Please check backend logs.');
                    setLoading(false);
                    return;
                }
                setLoading(false);
            }
            // Save config
            setLoading(true);
            try {
                await axios.post('/api/wizard/save', config);
                
                // Check engine status for completion step
                try {
                    const statusRes = await axios.get('/api/wizard/engine-status');
                    setEngineStatus({
                        running: statusRes.data.running,
                        exists: statusRes.data.exists,
                        checked: true
                    });
                } catch {
                    setEngineStatus({ running: false, exists: false, checked: true });
                }
                
                setStep(5); // Go to completion step
            } catch (err: any) {
                setError(err.response?.data?.detail || err.message);
            } finally {
                setLoading(false);
            }
        } else if (step === 2) {
            // Initialize .env when moving from provider selection to API keys step
            try {
                await axios.post('/api/wizard/init-env');
            } catch {
                // Non-fatal - continue anyway
            }
            setStep(step + 1);
        } else {
            setStep(step + 1);
        }
    };

    const ProviderCard = ({ id, title, description, icon: Icon, recommended = false }: any) => (
        <div
            onClick={() => setConfig({ ...config, provider: id })}
            className={`relative p-6 rounded-lg border-2 cursor-pointer transition-all ${config.provider === id
                ? 'border-primary bg-primary/5'
                : 'border-border hover:border-primary/50'
                }`}
        >
            {recommended && (
                <div className="absolute -top-3 left-4 bg-primary text-primary-foreground text-xs px-2 py-1 rounded-full">
                    Recommended
                </div>
            )}
            <div className="flex items-start space-x-4">
                <div className={`p-2 rounded-lg ${config.provider === id ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}`}>
                    <Icon className="w-6 h-6" />
                </div>
                <div>
                    <h3 className="font-semibold text-lg">{title}</h3>
                    <p className="text-sm text-muted-foreground mt-1">{description}</p>
                </div>
            </div>
        </div>
    );

    return (
        <div className="min-h-screen bg-background flex items-center justify-center p-4">
            <div className="max-w-3xl w-full bg-card border border-border rounded-lg shadow-lg p-8">
                <div className="mb-8 flex justify-between items-start">
                    <div>
                        <h1 className="text-3xl font-bold text-foreground mb-2">Setup Wizard</h1>
                        <div className="flex items-center space-x-2 text-sm text-muted-foreground overflow-x-auto">
                            <span className={step >= 1 ? "text-primary font-medium whitespace-nowrap" : "whitespace-nowrap"}>1. Welcome</span>
                            <span>&rarr;</span>
                            <span className={step >= 2 ? "text-primary font-medium whitespace-nowrap" : "whitespace-nowrap"}>2. Provider</span>
                            <span>&rarr;</span>
                            <span className={step >= 3 ? "text-primary font-medium whitespace-nowrap" : "whitespace-nowrap"}>3. API Keys</span>
                            <span>&rarr;</span>
                            <span className={step >= 4 ? "text-primary font-medium whitespace-nowrap" : "whitespace-nowrap"}>4. Config</span>
                            {step === 5 && (
                                <>
                                    <span>&rarr;</span>
                                    <span className="text-primary font-medium whitespace-nowrap">5. Done</span>
                                </>
                            )}
                        </div>
                    </div>
                    {step === 1 && (
                        <button
                            type="button"
                            onClick={handleSkip}
                            className="text-sm text-muted-foreground hover:text-foreground flex items-center"
                        >
                            <SkipForward className="w-4 h-4 mr-1" />
                            Skip Setup
                        </button>
                    )}
                </div>

                {error && (
                    <div className="mb-6 p-4 bg-destructive/10 border border-destructive/20 rounded-md flex items-center text-destructive">
                        <AlertCircle className="w-5 h-5 mr-2" />
                        {error}
                    </div>
                )}

                {step === 1 && (
                    <div className="space-y-6">
                        <div className="prose dark:prose-invert">
                            <p className="text-lg">Welcome to the Asterisk AI Voice Agent setup.</p>
                            <p>This wizard will help you configure the essential settings to get your agent up and running in minutes.</p>
                            <div className="bg-muted p-4 rounded-lg">
                                <h3 className="font-medium mb-2">You will need:</h3>
                                <ul className="list-disc list-inside space-y-1">
                                    <li>API Keys (OpenAI, Deepgram, or Google)</li>
                                    <li>Asterisk Connection Details (Host, Username, Password)</li>
                                </ul>
                            </div>
                        </div>
                    </div>
                )}

                {step === 2 && (
                    <div className="space-y-4">
                        <h2 className="text-xl font-semibold mb-4">Select Your AI Provider</h2>
                        <div className="grid gap-4">
                            <ProviderCard
                                id="google_live"
                                title="Google Gemini Live"
                                description="Real-time bidirectional streaming. Native audio processing, ultra-low latency (<1s)."
                                icon={Zap}
                                recommended={true}
                            />
                            <ProviderCard
                                id="openai_realtime"
                                title="OpenAI Realtime"
                                description="Fastest setup, natural conversations. Uses OpenAI's Realtime API for low-latency voice interactions."
                                icon={Cloud}
                            />
                            <ProviderCard
                                id="deepgram"
                                title="Deepgram Voice Agent"
                                description="Enterprise-grade with 'Think' stage. Best for complex queries and high reliability."
                                icon={Server}
                            />
                            <ProviderCard
                                id="local_hybrid"
                                title="Local Hybrid"
                                description="Privacy-focused. Audio stays local (STT/TTS), only text is sent to cloud LLM."
                                icon={Shield}
                            />
                            <ProviderCard
                                id="local"
                                title="Local (Full)"
                                description="100% on-premises. All processing stays local - STT, LLM, and TTS. No API keys required."
                                icon={HardDrive}
                            />
                        </div>
                    </div>
                )}

                {step === 3 && (
                    <div className="space-y-4">
                        <h2 className="text-xl font-semibold mb-4">Configure API Keys</h2>

                        {(config.provider === 'openai_realtime' || config.provider === 'local_hybrid') && (
                            <div className="space-y-4">
                                {config.provider === 'local_hybrid' && (
                                    <div className="space-y-3">
                                        <div className="bg-blue-50/50 dark:bg-blue-900/10 p-4 rounded-md border border-blue-100 dark:border-blue-900/20 text-sm text-blue-800 dark:text-blue-300">
                                            <p className="font-semibold mb-1 flex items-center gap-2">
                                                <Server className="w-4 h-4" />
                                                Local Server Required
                                            </p>
                                            <p>
                                                The Local Hybrid mode requires the <code>local-ai-server</code> container to be running.
                                                The wizard will attempt to start it, but ensure you have built the image.
                                            </p>
                                        </div>
                                        
                                        {/* Check for existing models button */}
                                        <div className="flex items-center gap-2">
                                            <button
                                                onClick={async () => {
                                                    try {
                                                        const res = await axios.get('/api/wizard/local/models-status');
                                                        setLocalAIStatus(prev => ({
                                                            ...prev,
                                                            existingModels: {
                                                                stt: res.data.stt_models || [],
                                                                llm: res.data.llm_models || [],
                                                                tts: res.data.tts_models || []
                                                            },
                                                            modelsReady: res.data.ready
                                                        }));
                                                    } catch (err) {}
                                                }}
                                                className="text-xs px-2 py-1 rounded bg-secondary text-secondary-foreground hover:bg-secondary/80"
                                            >
                                                Check Existing Models
                                            </button>
                                        </div>
                                        
                                        {/* Warning if models already exist */}
                                        {localAIStatus.modelsReady && (
                                            <div className="p-3 bg-yellow-50 dark:bg-yellow-900/20 rounded border border-yellow-200 dark:border-yellow-800 text-sm">
                                                <p className="font-medium text-yellow-800 dark:text-yellow-300 flex items-center">
                                                    <AlertTriangle className="w-4 h-4 mr-2" />
                                                    Existing Models Detected
                                                </p>
                                                <ul className="text-xs text-yellow-600 dark:text-yellow-500 mt-1 ml-6 list-disc">
                                                    {localAIStatus.existingModels.stt.length > 0 && <li>STT: {localAIStatus.existingModels.stt.join(', ')}</li>}
                                                    {localAIStatus.existingModels.llm.length > 0 && <li>LLM: {localAIStatus.existingModels.llm.join(', ')}</li>}
                                                    {localAIStatus.existingModels.tts.length > 0 && <li>TTS: {localAIStatus.existingModels.tts.join(', ')}</li>}
                                                </ul>
                                                <p className="text-yellow-700 dark:text-yellow-400 mt-1 text-xs">
                                                    ⚠️ Re-running model setup will overwrite these.
                                                </p>
                                            </div>
                                        )}
                                    </div>
                                )}
                                <div className="space-y-2">
                                    <label className="text-sm font-medium">
                                        OpenAI API Key
                                        {config.provider === 'local_hybrid' && <span className="text-muted-foreground font-normal ml-2">(for LLM only)</span>}
                                    </label>
                                    <div className="flex space-x-2">
                                        <input
                                            type="password"
                                            className="w-full p-2 rounded-md border border-input bg-background"
                                            value={config.openai_key}
                                            onChange={e => setConfig({ ...config, openai_key: e.target.value })}
                                            placeholder="sk-..."
                                        />
                                        <button
                                            onClick={() => handleTestKey('openai', config.openai_key || '')}
                                            className="px-3 py-2 rounded-md bg-secondary text-secondary-foreground hover:bg-secondary/80"
                                            disabled={loading}
                                        >
                                            Test
                                        </button>
                                    </div>
                                    <p className="text-xs text-muted-foreground">Required for OpenAI Realtime and Local Hybrid providers.</p>
                                </div>
                            </div>
                        )}

                        {config.provider === 'deepgram' && (
                            <div className="space-y-2">
                                <label className="text-sm font-medium">Deepgram API Key</label>
                                <div className="flex space-x-2">
                                    <input
                                        type="password"
                                        className="w-full p-2 rounded-md border border-input bg-background"
                                        value={config.deepgram_key}
                                        onChange={e => setConfig({ ...config, deepgram_key: e.target.value })}
                                        placeholder="Token..."
                                    />
                                    <button
                                        onClick={() => handleTestKey('deepgram', config.deepgram_key || '')}
                                        className="px-3 py-2 rounded-md bg-secondary text-secondary-foreground hover:bg-secondary/80"
                                        disabled={loading}
                                    >
                                        Test
                                    </button>
                                </div>
                                <p className="text-xs text-muted-foreground">Required for Deepgram Voice Agent provider.</p>
                            </div>
                        )}

                        {config.provider === 'google_live' && (
                            <div className="space-y-2">
                                <label className="text-sm font-medium">Google API Key</label>
                                <div className="flex space-x-2">
                                    <input
                                        type="password"
                                        className="w-full p-2 rounded-md border border-input bg-background"
                                        value={config.google_key}
                                        onChange={e => setConfig({ ...config, google_key: e.target.value })}
                                        placeholder="AIza..."
                                    />
                                    <button
                                        onClick={() => handleTestKey('google', config.google_key || '')}
                                        className="px-3 py-2 rounded-md bg-secondary text-secondary-foreground hover:bg-secondary/80"
                                        disabled={loading}
                                    >
                                        Test
                                    </button>
                                </div>
                                <p className="text-xs text-muted-foreground">Required for Google Gemini Live provider.</p>
                            </div>
                        )}

                        {config.provider === 'local' && (
                            <div className="space-y-4">
                                <div className="bg-green-50/50 dark:bg-green-900/10 p-4 rounded-md border border-green-100 dark:border-green-900/20">
                                    <p className="font-semibold mb-2 flex items-center gap-2 text-green-800 dark:text-green-300">
                                        <HardDrive className="w-4 h-4" />
                                        Local AI Server Setup
                                    </p>
                                    <p className="text-sm text-green-700 dark:text-green-400 mb-3">
                                        Local (Full) mode runs entirely on your infrastructure. No API keys required.
                                    </p>
                                </div>

                                {/* System Detection */}
                                <div className="bg-muted p-4 rounded-lg">
                                    <div className="flex justify-between items-center mb-3">
                                        <h4 className="font-medium">System Detection</h4>
                                        <button
                                            onClick={async () => {
                                                setLoading(true);
                                                try {
                                                    // Detect tier and check existing models in parallel
                                                    const [tierRes, modelsRes] = await Promise.all([
                                                        axios.get('/api/wizard/local/detect-tier'),
                                                        axios.get('/api/wizard/local/models-status')
                                                    ]);
                                                    setLocalAIStatus(prev => ({
                                                        ...prev,
                                                        tier: tierRes.data.tier,
                                                        tierInfo: tierRes.data.tier_info,
                                                        cpuCores: tierRes.data.cpu_cores,
                                                        ramGb: tierRes.data.ram_gb,
                                                        gpuDetected: tierRes.data.gpu_detected,
                                                        existingModels: {
                                                            stt: modelsRes.data.stt_models || [],
                                                            llm: modelsRes.data.llm_models || [],
                                                            tts: modelsRes.data.tts_models || []
                                                        },
                                                        modelsReady: modelsRes.data.ready
                                                    }));
                                                } catch (err: any) {
                                                    setError('Failed to detect system: ' + err.message);
                                                }
                                                setLoading(false);
                                            }}
                                            disabled={loading}
                                            className="px-3 py-1 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                                        >
                                            {loading ? 'Detecting...' : 'Detect System'}
                                        </button>
                                    </div>
                                    
                                    {localAIStatus.tier && (
                                        <div className="space-y-2 text-sm">
                                            <div className="grid grid-cols-3 gap-2">
                                                <div className="p-2 bg-background rounded">
                                                    <span className="text-muted-foreground">CPU Cores:</span>
                                                    <span className="ml-2 font-medium">{localAIStatus.cpuCores}</span>
                                                </div>
                                                <div className="p-2 bg-background rounded">
                                                    <span className="text-muted-foreground">RAM:</span>
                                                    <span className="ml-2 font-medium">{localAIStatus.ramGb} GB</span>
                                                </div>
                                                <div className="p-2 bg-background rounded">
                                                    <span className="text-muted-foreground">GPU:</span>
                                                    <span className="ml-2 font-medium">{localAIStatus.gpuDetected ? 'Yes' : 'No'}</span>
                                                </div>
                                            </div>
                                            <div className="p-3 bg-blue-50 dark:bg-blue-900/20 rounded border border-blue-200 dark:border-blue-800">
                                                <p className="font-medium text-blue-800 dark:text-blue-300">
                                                    Recommended Tier: {localAIStatus.tier}
                                                </p>
                                                <p className="text-blue-700 dark:text-blue-400 mt-1">
                                                    {localAIStatus.tierInfo?.models}
                                                </p>
                                                <p className="text-xs text-blue-600 dark:text-blue-500 mt-1">
                                                    Performance: {localAIStatus.tierInfo?.performance} | 
                                                    Download: {localAIStatus.tierInfo?.download_size}
                                                </p>
                                            </div>
                                            
                                            {/* Warning if models already exist */}
                                            {localAIStatus.modelsReady && (
                                                <div className="p-3 bg-yellow-50 dark:bg-yellow-900/20 rounded border border-yellow-200 dark:border-yellow-800">
                                                    <p className="font-medium text-yellow-800 dark:text-yellow-300 flex items-center">
                                                        <AlertTriangle className="w-4 h-4 mr-2" />
                                                        Existing Models Detected
                                                    </p>
                                                    <p className="text-yellow-700 dark:text-yellow-400 mt-1 text-xs">
                                                        Models are already downloaded on this system:
                                                    </p>
                                                    <ul className="text-xs text-yellow-600 dark:text-yellow-500 mt-1 ml-4 list-disc">
                                                        {localAIStatus.existingModels.stt.length > 0 && (
                                                            <li>STT: {localAIStatus.existingModels.stt.join(', ')}</li>
                                                        )}
                                                        {localAIStatus.existingModels.llm.length > 0 && (
                                                            <li>LLM: {localAIStatus.existingModels.llm.join(', ')}</li>
                                                        )}
                                                        {localAIStatus.existingModels.tts.length > 0 && (
                                                            <li>TTS: {localAIStatus.existingModels.tts.join(', ')}</li>
                                                        )}
                                                    </ul>
                                                    <p className="text-yellow-700 dark:text-yellow-400 mt-2 text-xs font-medium">
                                                        ⚠️ Downloading new models will overwrite existing ones.
                                                    </p>
                                                </div>
                                            )}
                                        </div>
                                    )}
                                </div>

                                {/* Model Download */}
                                {localAIStatus.tier && (
                                    <div className="bg-muted p-4 rounded-lg">
                                        <div className="flex justify-between items-center mb-3">
                                            <h4 className="font-medium">Download Models</h4>
                                            <button
                                                onClick={async () => {
                                                    setLocalAIStatus(prev => ({ ...prev, downloading: true, downloadOutput: [] }));
                                                    try {
                                                        await axios.post('/api/wizard/local/download-models', null, {
                                                            params: { tier: localAIStatus.tier }
                                                        });
                                                        // Poll for progress
                                                        const pollProgress = async () => {
                                                            try {
                                                                const res = await axios.get('/api/wizard/local/download-progress');
                                                                setLocalAIStatus(prev => ({
                                                                    ...prev,
                                                                    downloadOutput: res.data.output || []
                                                                }));
                                                                
                                                                if (res.data.completed) {
                                                                    setLocalAIStatus(prev => ({
                                                                        ...prev,
                                                                        downloading: false,
                                                                        downloadCompleted: true,
                                                                        modelsReady: true
                                                                    }));
                                                                } else if (res.data.error) {
                                                                    setError('Download failed: ' + res.data.error);
                                                                    setLocalAIStatus(prev => ({ ...prev, downloading: false }));
                                                                } else if (res.data.running) {
                                                                    setTimeout(pollProgress, 2000);
                                                                }
                                                            } catch (err) {
                                                                setTimeout(pollProgress, 3000);
                                                            }
                                                        };
                                                        setTimeout(pollProgress, 1000);
                                                    } catch (err: any) {
                                                        setError('Failed to start download: ' + err.message);
                                                        setLocalAIStatus(prev => ({ ...prev, downloading: false }));
                                                    }
                                                }}
                                                disabled={localAIStatus.downloading || localAIStatus.downloadCompleted}
                                                className="px-3 py-1 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                                            >
                                                {localAIStatus.downloading ? 'Downloading...' : localAIStatus.downloadCompleted ? 'Download Complete' : 'Download Models'}
                                            </button>
                                        </div>
                                        
                                        {/* Download Progress Output */}
                                        {localAIStatus.downloading && (
                                            <div className="mt-3">
                                                <div className="flex items-center gap-2 mb-2">
                                                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-primary"></div>
                                                    <span className="text-sm text-muted-foreground">Downloading models...</span>
                                                </div>
                                                <div className="bg-black/90 rounded p-3 max-h-48 overflow-y-auto font-mono text-xs text-green-400">
                                                    {localAIStatus.downloadOutput.length > 0 ? (
                                                        localAIStatus.downloadOutput.map((line, i) => (
                                                            <div key={i} className="whitespace-pre-wrap">{line}</div>
                                                        ))
                                                    ) : (
                                                        <div className="text-gray-500">Starting download...</div>
                                                    )}
                                                </div>
                                            </div>
                                        )}
                                        
                                        {localAIStatus.downloadCompleted && (
                                            <p className="text-sm text-green-600 dark:text-green-400 flex items-center mt-2">
                                                <CheckCircle className="w-4 h-4 mr-2" />
                                                Models downloaded successfully! Click Next to start the Local AI Server.
                                            </p>
                                        )}
                                    </div>
                                )}
                            </div>
                        )}

                        <div className="space-y-2 pt-4 border-t border-border">
                            <label className="text-sm font-medium">ElevenLabs API Key (Optional)</label>
                            <input
                                type="password"
                                className="w-full p-2 rounded-md border border-input bg-background"
                                value={config.elevenlabs_key || ''}
                                onChange={e => setConfig({ ...config, elevenlabs_key: e.target.value })}
                                placeholder="xi-..."
                            />
                        </div>
                    </div>
                )}

                {step === 4 && (
                    <div className="space-y-4">
                        <h2 className="text-xl font-semibold mb-4">Agent Configuration</h2>
                        <div className="grid grid-cols-2 gap-4">
                            <div className="space-y-2">
                                <label className="text-sm font-medium">Asterisk Host</label>
                                <input
                                    type="text"
                                    className="w-full p-2 rounded-md border border-input bg-background"
                                    value={config.asterisk_host}
                                    onChange={e => setConfig({ ...config, asterisk_host: e.target.value })}
                                />
                            </div>
                            <div className="space-y-2">
                                <label className="text-sm font-medium">ARI Username</label>
                                <input
                                    type="text"
                                    className="w-full p-2 rounded-md border border-input bg-background"
                                    value={config.asterisk_username}
                                    onChange={e => setConfig({ ...config, asterisk_username: e.target.value })}
                                />
                            </div>
                            <div className="space-y-2">
                                <label className="text-sm font-medium">ARI Port</label>
                                <input
                                    type="number"
                                    className="w-full p-2 rounded-md border border-input bg-background"
                                    value={config.asterisk_port}
                                    onChange={e => setConfig({ ...config, asterisk_port: parseInt(e.target.value) || 8088 })}
                                />
                            </div>
                            <div className="space-y-2">
                                <label className="text-sm font-medium">ARI Scheme</label>
                                <select
                                    className="w-full p-2 rounded-md border border-input bg-background"
                                    value={config.asterisk_scheme}
                                    onChange={e => setConfig({ ...config, asterisk_scheme: e.target.value })}
                                >
                                    <option value="http">http</option>
                                    <option value="https">https</option>
                                </select>
                            </div>
                            <div className="space-y-2">
                                <label className="text-sm font-medium">Stasis App Name</label>
                                <input
                                    type="text"
                                    className="w-full p-2 rounded-md border border-input bg-background"
                                    value={config.asterisk_app}
                                    onChange={e => setConfig({ ...config, asterisk_app: e.target.value })}
                                />
                            </div>
                        </div>
                        <div className="space-y-2">
                            <label className="text-sm font-medium">ARI Password</label>
                            <input
                                type="password"
                                className="w-full p-2 rounded-md border border-input bg-background"
                                value={config.asterisk_password}
                                onChange={e => setConfig({ ...config, asterisk_password: e.target.value })}
                            />
                        </div>
                        <div className="flex justify-end gap-2">
                            {config.provider === 'local_hybrid' && (
                                <button
                                    onClick={async () => {
                                        setLoading(true);
                                        try {
                                            const res = await axios.get('/api/system/health');
                                            if (res.data.local_ai_server?.status === 'connected') {
                                                alert('Local AI Server is running and connected!');
                                            } else {
                                                alert(`Local AI Server is NOT connected. Status: ${res.data.local_ai_server?.status}`);
                                            }
                                        } catch (err) {
                                            alert('Failed to contact system health endpoint.');
                                        } finally {
                                            setLoading(false);
                                        }
                                    }}
                                    className="px-3 py-2 text-sm rounded-md border border-input hover:bg-accent hover:text-accent-foreground flex items-center"
                                    disabled={loading}
                                >
                                    {loading ? <Loader2 className="w-3 h-3 mr-2 animate-spin" /> : <Server className="w-3 h-3 mr-2" />}
                                    Check Local Server
                                </button>
                            )}
                            <button
                                onClick={handleTestConnection}
                                className="px-3 py-2 text-sm rounded-md bg-secondary text-secondary-foreground hover:bg-secondary/80 flex items-center"
                                disabled={loading}
                            >
                                {loading ? <Loader2 className="w-3 h-3 mr-2 animate-spin" /> : <Zap className="w-3 h-3 mr-2" />}
                                Test Connection
                            </button>
                        </div>
                        <div className="border-t border-border my-4 pt-4"></div>
                        <div className="space-y-2">
                            <label className="text-sm font-medium">AI Name</label>
                            <input
                                type="text"
                                className="w-full p-2 rounded-md border border-input bg-background"
                                value={config.ai_name}
                                onChange={e => setConfig({ ...config, ai_name: e.target.value })}
                            />
                        </div>
                        <div className="space-y-2">
                            <label className="text-sm font-medium">AI Role</label>
                            <input
                                type="text"
                                className="w-full p-2 rounded-md border border-input bg-background"
                                value={config.ai_role}
                                onChange={e => setConfig({ ...config, ai_role: e.target.value })}
                            />
                        </div>
                        <div className="space-y-2">
                            <label className="text-sm font-medium">Greeting Message</label>
                            <textarea
                                className="w-full p-2 rounded-md border border-input bg-background min-h-[80px]"
                                value={config.greeting}
                                onChange={e => setConfig({ ...config, greeting: e.target.value })}
                            />
                        </div>
                    </div>
                )}

                {step === 5 && (
                    <div className="space-y-6 text-center">
                        <div className="w-16 h-16 bg-green-100 text-green-600 rounded-full flex items-center justify-center mx-auto mb-4">
                            <CheckCircle className="w-8 h-8" />
                        </div>
                        <h2 className="text-2xl font-bold">Setup Complete!</h2>
                        <p className="text-muted-foreground">
                            Your AI Agent is configured and ready.
                        </p>

                        {/* Local AI Server Setup - Only for Local provider */}
                        {config.provider === 'local' && (
                            <div className="space-y-4 text-left">
                                {/* Downloaded Models */}
                                <div className="bg-green-50 dark:bg-green-900/20 p-4 rounded-lg border border-green-200 dark:border-green-800">
                                    <h3 className="font-semibold mb-2 flex items-center text-green-800 dark:text-green-300">
                                        <HardDrive className="w-4 h-4 mr-2" />
                                        Downloaded Models
                                    </h3>
                                    <p className="text-sm text-green-700 dark:text-green-400">
                                        Tier: {localAIStatus.tier} | {localAIStatus.tierInfo?.models || 'Models ready'}
                                    </p>
                                </div>

                                {/* Start Local AI Server */}
                                <div className="bg-blue-50 dark:bg-blue-900/20 p-4 rounded-lg border border-blue-200 dark:border-blue-800">
                                    <h3 className="font-semibold mb-3 flex items-center text-blue-800 dark:text-blue-300">
                                        <Server className="w-4 h-4 mr-2" />
                                        Local AI Server
                                    </h3>
                                    
                                    {!localAIStatus.serverStarted ? (
                                        <button
                                            onClick={async () => {
                                                setLoading(true);
                                                try {
                                                    const res = await axios.post('/api/wizard/local/start-server');
                                                    if (res.data.success) {
                                                        setLocalAIStatus(prev => ({ ...prev, serverStarted: true }));
                                                        // Start polling logs
                                                        const pollLogs = async () => {
                                                            try {
                                                                const logRes = await axios.get('/api/wizard/local/server-logs');
                                                                setLocalAIStatus(prev => ({
                                                                    ...prev,
                                                                    serverLogs: logRes.data.logs || [],
                                                                    serverReady: logRes.data.ready
                                                                }));
                                                                if (!logRes.data.ready) {
                                                                    setTimeout(pollLogs, 2000);
                                                                }
                                                            } catch {
                                                                setTimeout(pollLogs, 3000);
                                                            }
                                                        };
                                                        setTimeout(pollLogs, 2000);
                                                    } else {
                                                        setError(res.data.message);
                                                    }
                                                } catch (err: any) {
                                                    setError('Failed to start server: ' + err.message);
                                                }
                                                setLoading(false);
                                            }}
                                            disabled={loading}
                                            className="w-full px-4 py-2 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 flex items-center justify-center"
                                        >
                                            {loading ? (
                                                <>
                                                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                                                    Starting...
                                                </>
                                            ) : (
                                                <>
                                                    <Play className="w-4 h-4 mr-2" />
                                                    Start Local AI Server
                                                </>
                                            )}
                                        </button>
                                    ) : (
                                        <div className="space-y-3">
                                            <div className="flex items-center gap-2">
                                                {localAIStatus.serverReady ? (
                                                    <span className="text-green-600 dark:text-green-400 flex items-center">
                                                        <CheckCircle className="w-4 h-4 mr-2" />
                                                        Server Ready!
                                                    </span>
                                                ) : (
                                                    <span className="text-blue-600 dark:text-blue-400 flex items-center">
                                                        <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-blue-600 mr-2"></div>
                                                        Starting up... (loading models)
                                                    </span>
                                                )}
                                            </div>
                                            
                                            {/* Server Logs */}
                                            <div className="bg-black/90 rounded p-3 max-h-48 overflow-y-auto font-mono text-xs text-green-400">
                                                {localAIStatus.serverLogs.length > 0 ? (
                                                    localAIStatus.serverLogs.map((line, i) => (
                                                        <div key={i} className="whitespace-pre-wrap">{line}</div>
                                                    ))
                                                ) : (
                                                    <div className="text-gray-500">Waiting for logs...</div>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                </div>

                                {/* Go to Dashboard - Only when server is ready */}
                                {localAIStatus.serverReady && (
                                    <div className="pt-4">
                                        <button
                                            onClick={() => navigate('/')}
                                            className="w-full px-4 py-3 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 font-medium"
                                        >
                                            Go to Dashboard
                                        </button>
                                    </div>
                                )}

                                {/* Dialplan for Local */}
                                {localAIStatus.serverReady && (
                                    <div className="bg-muted p-4 rounded-lg">
                                        <h3 className="font-semibold mb-2 flex items-center">
                                            <Terminal className="w-4 h-4 mr-2" />
                                            Asterisk Dialplan for Local Provider
                                        </h3>
                                        <pre className="bg-black text-green-400 p-3 rounded-md overflow-x-auto text-xs font-mono">
{`[from-ai-agent-local]
exten => s,1,NoOp(AI Agent - Local Full)
 same => n,Set(AI_CONTEXT=default)
 same => n,Set(AI_PROVIDER=local)
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()`}
                                        </pre>
                                    </div>
                                )}
                            </div>
                        )}

                        {/* AI Engine Status - Show start button if not running (non-local providers) */}
                        {config.provider !== 'local' && engineStatus.checked && !engineStatus.running && (
                            <div className="bg-blue-50 dark:bg-blue-900/20 p-4 rounded-lg text-left border border-blue-200 dark:border-blue-800">
                                <h3 className="font-semibold mb-3 flex items-center text-blue-800 dark:text-blue-300">
                                    <Server className="w-4 h-4 mr-2" />
                                    Start AI Engine
                                </h3>
                                <p className="text-sm text-blue-700 dark:text-blue-400 mb-4">
                                    {engineStatus.exists 
                                        ? "The AI Engine container exists but is not running. Click below to start it."
                                        : "The AI Engine container needs to be created. Run the command below, then click Start."}
                                </p>
                                {!engineStatus.exists && (
                                    <pre className="bg-black text-green-400 p-3 rounded-md text-xs font-mono mb-4 overflow-x-auto">
                                        docker-compose up -d ai-engine
                                    </pre>
                                )}
                                <button
                                    onClick={async () => {
                                        setStartingEngine(true);
                                        setError(null);
                                        try {
                                            const res = await axios.post('/api/wizard/start-engine');
                                            if (res.data.success) {
                                                setEngineStatus({ ...engineStatus, running: true, exists: true });
                                            } else {
                                                setError(res.data.message);
                                            }
                                        } catch (err: any) {
                                            setError(err.response?.data?.detail || err.message);
                                        } finally {
                                            setStartingEngine(false);
                                        }
                                    }}
                                    disabled={startingEngine}
                                    className="w-full px-4 py-2 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 flex items-center justify-center"
                                >
                                    {startingEngine ? (
                                        <>
                                            <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                                            Starting...
                                        </>
                                    ) : (
                                        <>
                                            <Play className="w-4 h-4 mr-2" />
                                            Start AI Engine
                                        </>
                                    )}
                                </button>
                            </div>
                        )}

                        {/* Engine Running - Success (non-local providers) */}
                        {config.provider !== 'local' && engineStatus.checked && engineStatus.running && (
                            <div className="bg-green-50 dark:bg-green-900/20 p-4 rounded-lg text-left border border-green-200 dark:border-green-800">
                                <div className="flex items-center text-green-700 dark:text-green-400">
                                    <CheckCircle className="w-5 h-5 mr-2" />
                                    <span className="font-medium">AI Engine is running</span>
                                </div>
                            </div>
                        )}

                        {/* Dialplan Section - non-local providers */}
                        {config.provider !== 'local' && (
                        <>
                        <div className="bg-muted p-4 rounded-lg text-left">
                            <h3 className="font-semibold mb-2 flex items-center">
                                <Terminal className="w-4 h-4 mr-2" />
                                Next Step: Update Asterisk Dialplan
                            </h3>
                            <p className="text-sm text-muted-foreground mb-3">
                                Add this to your <code>extensions_custom.conf</code> to route calls to the agent:
                            </p>
                            <div className="relative group">
                                <pre className="bg-black text-green-400 p-4 rounded-md overflow-x-auto text-sm font-mono">
                                    {`; extensions_custom.conf
[from-ai-agent]
exten => s,1,NoOp(AI Agent Call)
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()`}
                                </pre>
                                <button
                                    onClick={() => {
                                        const dialplan = `; extensions_custom.conf
[from-ai-agent]
exten => s,1,NoOp(AI Agent Call)
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()`;
                                        navigator.clipboard.writeText(dialplan);
                                    }}
                                    className="absolute top-2 right-2 p-1 bg-white/10 rounded hover:bg-white/20 text-white opacity-0 group-hover:opacity-100 transition-opacity"
                                    title="Copy to clipboard"
                                >
                                    <Copy className="w-4 h-4" />
                                </button>
                            </div>
                        </div>

                        <div className="pt-4">
                            <button
                                onClick={() => navigate('/')}
                                className="w-full px-4 py-3 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 font-medium"
                            >
                                Go to Dashboard
                            </button>
                        </div>
                        </>
                        )}
                    </div>
                )}

                <div className="mt-8 flex justify-between">
                    {step > 1 && step < 5 ? (
                        <button
                            onClick={() => setStep(step - 1)}
                            className="px-4 py-2 rounded-md border border-input hover:bg-accent hover:text-accent-foreground"
                            disabled={loading}
                        >
                            Back
                        </button>
                    ) : <div></div>}

                    {step < 5 && (
                        <button
                            onClick={handleNext}
                            disabled={loading || (config.provider === 'local' && step === 3 && (localAIStatus.downloading || (!localAIStatus.downloadCompleted && !!localAIStatus.tier)))}
                            className="px-4 py-2 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 flex items-center disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {loading && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
                            {step === 4 ? 'Finish Setup' : 'Next'}
                            {step < 4 && <ArrowRight className="w-4 h-4 ml-2" />}
                        </button>
                    )}
                </div>
                {showSkipConfirm && (
                    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
                        <div className="bg-card border border-border p-6 rounded-lg shadow-lg max-w-md w-full">
                            <h3 className="text-lg font-semibold mb-2">Skip Setup?</h3>
                            <p className="text-muted-foreground mb-4">
                                Are you sure you want to skip setup? You will need to manually configure the environment variables later.
                            </p>
                            <div className="flex justify-end space-x-2">
                                <button
                                    onClick={() => setShowSkipConfirm(false)}
                                    className="px-4 py-2 rounded-md border border-input hover:bg-accent hover:text-accent-foreground"
                                >
                                    Cancel
                                </button>
                                <button
                                    onClick={confirmSkip}
                                    className="px-4 py-2 rounded-md bg-destructive text-destructive-foreground hover:bg-destructive/90"
                                >
                                    Skip Setup
                                </button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div >
    );
};

export default Wizard;
