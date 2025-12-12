import React, { useState, useEffect } from 'react';
import axios from 'axios';
import yaml from 'js-yaml';
import { Save, AlertCircle, RefreshCw, Loader2 } from 'lucide-react';
import { ConfigSection } from '../../components/ui/ConfigSection';
import { ConfigCard } from '../../components/ui/ConfigCard';
import { FormInput, FormSelect } from '../../components/ui/FormComponents';

const TransportPage = () => {
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
            alert('Transport configuration saved successfully');
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

    const updateConfig = (field: string, value: any) => {
        setConfig({ ...config, [field]: value });
    };

    const updateSectionConfig = (section: string, field: string, value: any) => {
        setConfig({
            ...config,
            [section]: {
                ...config[section],
                [field]: value
            }
        });
    };

    if (loading) return <div className="p-8 text-center text-muted-foreground">Loading configuration...</div>;

    const transportType = config.audio_transport || 'audiosocket';
    const audiosocketConfig = config.audiosocket || {};
    const externalMediaConfig = config.external_media || {};

    return (
        <div className="space-y-6">
            <div className={`${pendingRestart ? 'bg-orange-500/15 border-orange-500/30' : 'bg-yellow-500/10 border-yellow-500/20'} border text-yellow-600 dark:text-yellow-500 p-4 rounded-md flex items-center justify-between`}>
                <div className="flex items-center">
                    <AlertCircle className="w-5 h-5 mr-2" />
                    Changes to transport configurations require an AI Engine restart to take effect.
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
                    <h1 className="text-3xl font-bold tracking-tight">Audio Transport</h1>
                    <p className="text-muted-foreground mt-1">
                        Configure how audio is transported between Asterisk and the AI Agent.
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

            <ConfigSection title="Asterisk Configuration" description="Core Asterisk integration settings.">
                <ConfigCard>
                    <FormInput
                        label="Stasis Application Name"
                        value={config.asterisk?.app_name || 'asterisk-ai-voice-agent'}
                        onChange={(e) => updateSectionConfig('asterisk', 'app_name', e.target.value)}
                        tooltip="Name of the Stasis application in your dialplan. Must match the app name in your Asterisk configuration."
                    />
                </ConfigCard>
            </ConfigSection>

            <ConfigSection title="Transport Type" description="Select the audio transport method.">
                <ConfigCard>
                    <FormSelect
                        label="Transport Method"
                        value={transportType}
                        onChange={(e) => updateConfig('audio_transport', e.target.value)}
                        options={[
                            { value: 'audiosocket', label: 'AudioSocket (Default)' },
                            { value: 'externalmedia', label: 'External Media (RTP)' }
                        ]}
                        description="Choose 'AudioSocket' for standard deployments or 'External Media' for RTP-based integration."
                    />
                </ConfigCard>
            </ConfigSection>

            {transportType === 'audiosocket' && (
                <ConfigSection title="AudioSocket Settings" description="Configuration for the AudioSocket server.">
                    <ConfigCard>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                            <FormInput
                                label="Host"
                                value={audiosocketConfig.host || '127.0.0.1'}
                                onChange={(e) => updateSectionConfig('audiosocket', 'host', e.target.value)}
                                tooltip="IP address the AudioSocket server listens on (default: 127.0.0.1)."
                            />
                            <FormInput
                                label="Port"
                                type="number"
                                value={audiosocketConfig.port || 8090}
                                onChange={(e) => updateSectionConfig('audiosocket', 'port', parseInt(e.target.value))}
                                tooltip="TCP port for AudioSocket connections (default: 8090)."
                            />
                            <FormInput
                                label="Format"
                                value={audiosocketConfig.format || 'slin'}
                                onChange={(e) => updateSectionConfig('audiosocket', 'format', e.target.value)}
                                tooltip="Audio format (e.g., slin)"
                            />
                        </div>
                    </ConfigCard>
                </ConfigSection>
            )}

            {transportType === 'externalmedia' && (
                <ConfigSection title="External Media (RTP) Settings" description="Configuration for RTP-based audio transport.">
                    <ConfigCard>
                        <div className="space-y-6">
                            <h4 className="text-sm font-medium text-muted-foreground uppercase tracking-wider">Network Configuration</h4>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                <FormInput
                                    label="RTP Host"
                                    value={externalMediaConfig.rtp_host || '127.0.0.1'}
                                    onChange={(e) => updateSectionConfig('external_media', 'rtp_host', e.target.value)}
                                    tooltip="IP address for RTP media (default: 127.0.0.1 for localhost)."
                                />
                                <FormInput
                                    label="RTP Port"
                                    type="number"
                                    value={externalMediaConfig.rtp_port || 18080}
                                    onChange={(e) => updateSectionConfig('external_media', 'rtp_port', parseInt(e.target.value))}
                                    tooltip="Base UDP port for RTP streams (default: 18080)."
                                />
                                <FormInput
                                    label="Port Range"
                                    value={externalMediaConfig.port_range || '18080:18099'}
                                    onChange={(e) => updateSectionConfig('external_media', 'port_range', e.target.value)}
                                    placeholder="18080:18099"
                                    tooltip="Range of UDP ports for concurrent calls (format: start:end, e.g., 18080:18099)."
                                />
                            </div>

                            <div className="border-t border-border my-4"></div>

                            <h4 className="text-sm font-medium text-muted-foreground uppercase tracking-wider">Asterisk-side Configuration</h4>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                <FormSelect
                                    label="Codec"
                                    value={externalMediaConfig.codec || 'ulaw'}
                                    onChange={(e) => updateSectionConfig('external_media', 'codec', e.target.value)}
                                    options={[
                                        { value: 'ulaw', label: 'μ-law (8kHz)' },
                                        { value: 'alaw', label: 'A-law (8kHz)' },
                                        { value: 'slin', label: 'SLIN (8kHz)' },
                                        { value: 'slin16', label: 'SLIN16 (16kHz)' }
                                    ]}
                                    description="Codec Asterisk sends/receives."
                                />
                                <FormSelect
                                    label="Direction"
                                    value={externalMediaConfig.direction || 'both'}
                                    onChange={(e) => updateSectionConfig('external_media', 'direction', e.target.value)}
                                    options={[
                                        { value: 'both', label: 'Both' },
                                        { value: 'sendonly', label: 'Send Only' },
                                        { value: 'recvonly', label: 'Receive Only' }
                                    ]}
                                />
                            </div>

                            <div className="border-t border-border my-4"></div>

                            <h4 className="text-sm font-medium text-muted-foreground uppercase tracking-wider">Engine-side Configuration</h4>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                <FormSelect
                                    label="Internal Format"
                                    value={externalMediaConfig.format || 'slin16'}
                                    onChange={(e) => updateSectionConfig('external_media', 'format', e.target.value)}
                                    options={[
                                        { value: 'slin', label: 'SLIN (8kHz)' },
                                        { value: 'slin16', label: 'SLIN16 (16kHz)' },
                                        { value: 'ulaw', label: 'μ-law (8kHz)' }
                                    ]}
                                    description="Engine internal format. Pipelines typically expect 16kHz PCM16 (slin16)."
                                />
                                <FormInput
                                    label="Sample Rate (Hz)"
                                    type="number"
                                    value={externalMediaConfig.sample_rate || 16000}
                                    onChange={(e) => updateSectionConfig('external_media', 'sample_rate', parseInt(e.target.value))}
                                    tooltip="Auto-inferred from format if not set."
                                />
                            </div>
                        </div>
                    </ConfigCard>
                </ConfigSection>
            )}
        </div>
    );
};

export default TransportPage;
