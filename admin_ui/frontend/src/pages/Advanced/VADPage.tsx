import React, { useState, useEffect } from 'react';
import axios from 'axios';
import yaml from 'js-yaml';
import { Save, Activity, AlertCircle, RefreshCw, Loader2 } from 'lucide-react';
import { ConfigSection } from '../../components/ui/ConfigSection';
import { ConfigCard } from '../../components/ui/ConfigCard';
import { FormInput, FormSwitch } from '../../components/ui/FormComponents';

const VADPage = () => {
    const [config, setConfig] = useState<any>({});
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [pendingRestart, setPendingRestart] = useState(false);
    const [restartingEngine, setRestartingEngine] = useState(false);

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

    const handleSave = async () => {
        setSaving(true);
        try {
            await axios.post('/api/config/yaml', { content: yaml.dump(config) });
            setPendingRestart(true);
            alert('VAD configuration saved successfully');
        } catch (err) {
            console.error('Failed to save config', err);
            alert('Failed to save configuration');
        } finally {
            setSaving(false);
        }
    };

    const handleRestartAIEngine = async () => {
        setRestartingEngine(true);
        try {
            const response = await axios.post('/api/system/containers/ai_engine/restart');
            if (response.data.status === 'success') {
                setPendingRestart(false);
                alert('AI Engine restarted successfully. Changes are now active.');
            }
        } catch (error: any) {
            alert(`Failed to restart AI Engine: ${error.response?.data?.detail || error.message}`);
        } finally {
            setRestartingEngine(false);
        }
    };

    const updateVADConfig = (field: string, value: any) => {
        setConfig({
            ...config,
            vad: {
                ...config.vad,
                [field]: value
            }
        });
    };

    if (loading) return <div className="p-8 text-center text-muted-foreground">Loading configuration...</div>;

    const vadConfig = config.vad || {};

    return (
        <div className="space-y-6">
            <div className={`${pendingRestart ? 'bg-orange-500/15 border-orange-500/30' : 'bg-yellow-500/10 border-yellow-500/20'} border text-yellow-600 dark:text-yellow-500 p-4 rounded-md flex items-center justify-between`}>
                <div className="flex items-center">
                    <AlertCircle className="w-5 h-5 mr-2" />
                    Changes to VAD configurations require an AI Engine restart to take effect.
                </div>
                <button
                    onClick={handleRestartAIEngine}
                    disabled={restartingEngine}
                    className={`flex items-center text-xs px-3 py-1.5 rounded transition-colors ${
                        pendingRestart 
                            ? 'bg-orange-500 text-white hover:bg-orange-600 font-medium' 
                            : 'bg-yellow-500/20 hover:bg-yellow-500/30'
                    } disabled:opacity-50`}
                >
                    {restartingEngine ? (
                        <Loader2 className="w-3 h-3 mr-1.5 animate-spin" />
                    ) : (
                        <RefreshCw className="w-3 h-3 mr-1.5" />
                    )}
                    {restartingEngine ? 'Restarting...' : 'Reload AI Engine'}
                </button>
            </div>

            <div className="flex justify-between items-center">
                <div>
                    <h1 className="text-3xl font-bold tracking-tight">Voice Activity Detection</h1>
                    <p className="text-muted-foreground mt-1">
                        Configure how the system detects speech and silence.
                    </p>
                </div>
                <button
                    onClick={handleSave}
                    disabled={saving}
                    className="inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 bg-primary text-primary-foreground shadow hover:bg-primary/90 h-9 px-4 py-2"
                >
                    <Save className="w-4 h-4 mr-2" />
                    {saving ? 'Saving...' : 'Save Changes'}
                </button>
            </div>

            <ConfigSection title="Primary Detection" description="Main VAD settings for speech detection.">
                <ConfigCard>
                    <div className="space-y-6">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                            <FormSwitch
                                label="Enhanced VAD"
                                description="Use advanced algorithms for better accuracy."
                                checked={vadConfig.enhanced_enabled ?? true}
                                onChange={(e) => updateVADConfig('enhanced_enabled', e.target.checked)}
                            />
                            <FormSwitch
                                label="Use Provider VAD"
                                description="Offload VAD to the STT provider if supported."
                                checked={vadConfig.use_provider_vad ?? false}
                                onChange={(e) => updateVADConfig('use_provider_vad', e.target.checked)}
                            />
                        </div>

                        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                            <FormInput
                                label="Min Utterance Duration (ms)"
                                type="number"
                                value={vadConfig.min_utterance_duration_ms || 600}
                                onChange={(e) => updateVADConfig('min_utterance_duration_ms', parseInt(e.target.value))}
                                tooltip="Minimum speech duration to be considered valid - filters out noise (default: 600ms)."
                            />
                            <FormInput
                                label="Max Utterance Duration (ms)"
                                type="number"
                                value={vadConfig.max_utterance_duration_ms || 10000}
                                onChange={(e) => updateVADConfig('max_utterance_duration_ms', parseInt(e.target.value))}
                                tooltip="Maximum speech duration before forcing a cutoff (default: 10000ms = 10s)."
                            />
                            <FormInput
                                label="Utterance Padding (ms)"
                                type="number"
                                value={vadConfig.utterance_padding_ms || 200}
                                onChange={(e) => updateVADConfig('utterance_padding_ms', parseInt(e.target.value))}
                                tooltip="Extra silence added after speech ends to catch trailing words (default: 200ms)."
                            />
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                            <FormInput
                                label="Fallback Buffer Size"
                                type="number"
                                value={vadConfig.fallback_buffer_size || 128000}
                                onChange={(e) => updateVADConfig('fallback_buffer_size', parseInt(e.target.value))}
                                tooltip="Audio buffer size in bytes for fallback VAD (default: 128000)."
                            />
                            <FormInput
                                label="Fallback Interval (ms)"
                                type="number"
                                value={vadConfig.fallback_interval_ms || 4000}
                                onChange={(e) => updateVADConfig('fallback_interval_ms', parseInt(e.target.value))}
                                tooltip="Interval for fallback VAD checks when primary is uncertain (default: 4000ms)."
                            />
                        </div>
                    </div>
                </ConfigCard>
            </ConfigSection>

            <ConfigSection title="Fallback VAD (WebRTC)" description="Backup detection mechanism using WebRTC standards.">
                <ConfigCard>
                    <div className="space-y-6">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                            <FormSwitch
                                label="Enable Fallback VAD"
                                description="Use WebRTC VAD when primary detection is uncertain."
                                checked={vadConfig.fallback_enabled ?? true}
                                onChange={(e) => updateVADConfig('fallback_enabled', e.target.checked)}
                            />
                        </div>

                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                            <FormInput
                                label="Fallback Buffer Size (bytes)"
                                type="number"
                                value={vadConfig.fallback_buffer_size || 128000}
                                onChange={(e) => updateVADConfig('fallback_buffer_size', parseInt(e.target.value))}
                                tooltip="Audio buffer size in bytes for fallback VAD (default: 128000)."
                            />
                            <FormInput
                                label="Fallback Interval (ms)"
                                type="number"
                                value={vadConfig.fallback_interval_ms || 4000}
                                onChange={(e) => updateVADConfig('fallback_interval_ms', parseInt(e.target.value))}
                                tooltip="Interval for fallback VAD checks when primary is uncertain (default: 4000ms)."
                            />
                        </div>

                        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                            <FormInput
                                label="Aggressiveness (0-3)"
                                type="number"
                                min="0"
                                max="3"
                                value={vadConfig.webrtc_aggressiveness || 1}
                                onChange={(e) => updateVADConfig('webrtc_aggressiveness', parseInt(e.target.value))}
                                tooltip="Higher values mean more aggressive silence detection."
                            />
                            <FormInput
                                label="Start Frames"
                                type="number"
                                value={vadConfig.webrtc_start_frames || 3}
                                onChange={(e) => updateVADConfig('webrtc_start_frames', parseInt(e.target.value))}
                                tooltip="Number of speech frames needed to trigger speech start (default: 3)."
                            />
                            <FormInput
                                label="End Silence Frames"
                                type="number"
                                value={vadConfig.webrtc_end_silence_frames || 50}
                                onChange={(e) => updateVADConfig('webrtc_end_silence_frames', parseInt(e.target.value))}
                                tooltip="Silence frames needed to detect end of speech (default: 50)."
                            />
                        </div>
                    </div>
                </ConfigCard>
            </ConfigSection>
        </div>
    );
};

export default VADPage;
