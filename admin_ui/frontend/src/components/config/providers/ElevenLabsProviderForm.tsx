import React from 'react';
import { ExternalLink, Info } from 'lucide-react';

interface ElevenLabsProviderFormProps {
    config: any;
    onChange: (newConfig: any) => void;
}

const ElevenLabsProviderForm: React.FC<ElevenLabsProviderFormProps> = ({ config, onChange }) => {
    const handleChange = (field: string, value: any) => {
        onChange({ ...config, [field]: value });
    };

    return (
        <div className="space-y-6">
            {/* Info Banner */}
            <div className="bg-blue-50/50 dark:bg-blue-900/10 p-4 rounded-md border border-blue-100 dark:border-blue-900/20">
                <div className="flex items-start gap-3">
                    <Info className="w-5 h-5 text-blue-600 dark:text-blue-400 mt-0.5 flex-shrink-0" />
                    <div className="text-sm text-blue-800 dark:text-blue-300">
                        <p className="font-semibold mb-1">ElevenLabs Conversational AI</p>
                        <p className="text-blue-700 dark:text-blue-400">
                            This provider uses a pre-configured agent from your ElevenLabs dashboard.
                            Voice, system prompt, LLM model, and first message are all configured there.
                        </p>
                    </div>
                </div>
            </div>

            {/* Agent Configuration */}
            <div>
                <h4 className="font-semibold mb-3">Agent Configuration</h4>
                <div className="space-y-4">
                    <div className="space-y-2">
                        <label className="text-sm font-medium">
                            Agent ID
                            <span className="text-destructive ml-1">*</span>
                        </label>
                        <input
                            type="text"
                            className="w-full p-2 rounded border border-input bg-background font-mono text-sm"
                            value={config.agent_id || ''}
                            onChange={(e) => handleChange('agent_id', e.target.value)}
                            placeholder="agent_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                        />
                        <p className="text-xs text-muted-foreground">
                            Get this from your{' '}
                            <a
                                href="https://elevenlabs.io/app/agents"
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-primary hover:underline inline-flex items-center gap-1"
                            >
                                ElevenLabs Agents dashboard
                                <ExternalLink className="w-3 h-3" />
                            </a>
                        </p>
                    </div>

                    <div className="flex items-center space-x-2">
                        <input
                            type="checkbox"
                            id="enabled"
                            className="rounded border-input"
                            checked={config.enabled ?? true}
                            onChange={(e) => handleChange('enabled', e.target.checked)}
                        />
                        <label htmlFor="enabled" className="text-sm font-medium">Enabled</label>
                    </div>
                </div>
            </div>

            {/* Authentication */}
            <div>
                <h4 className="font-semibold mb-3">Authentication</h4>
                <div className="space-y-2">
                    <label className="text-sm font-medium">API Key (Environment Variable)</label>
                    <input
                        type="text"
                        className="w-full p-2 rounded border border-input bg-background"
                        value={config.api_key || '${ELEVENLABS_API_KEY}'}
                        onChange={(e) => handleChange('api_key', e.target.value)}
                        placeholder="${ELEVENLABS_API_KEY}"
                    />
                    <p className="text-xs text-muted-foreground">
                        Use {'${VAR_NAME}'} to reference environment variables. Set ELEVENLABS_API_KEY in your .env file.
                    </p>
                </div>
            </div>

            {/* Audio Settings (mostly fixed) */}
            <div>
                <h4 className="font-semibold mb-3">Audio Settings</h4>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="space-y-2">
                        <label className="text-sm font-medium">Input Sample Rate (Hz)</label>
                        <input
                            type="number"
                            className="w-full p-2 rounded border border-input bg-background"
                            value={config.input_sample_rate || 16000}
                            onChange={(e) => handleChange('input_sample_rate', parseInt(e.target.value))}
                        />
                        <p className="text-xs text-muted-foreground">ElevenLabs requires 16000 Hz</p>
                    </div>

                    <div className="space-y-2">
                        <label className="text-sm font-medium">Output Sample Rate (Hz)</label>
                        <input
                            type="number"
                            className="w-full p-2 rounded border border-input bg-background"
                            value={config.output_sample_rate || 16000}
                            onChange={(e) => handleChange('output_sample_rate', parseInt(e.target.value))}
                        />
                        <p className="text-xs text-muted-foreground">ElevenLabs outputs at 16000 Hz</p>
                    </div>
                </div>
            </div>

            {/* Setup Requirements */}
            <div className="bg-amber-50/50 dark:bg-amber-900/10 p-4 rounded-md border border-amber-100 dark:border-amber-900/20">
                <h4 className="font-semibold mb-2 text-amber-800 dark:text-amber-300">Setup Requirements</h4>
                <ul className="text-sm text-amber-700 dark:text-amber-400 space-y-1 list-disc list-inside">
                    <li>Create an agent at <a href="https://elevenlabs.io/app/agents" target="_blank" rel="noopener noreferrer" className="underline">elevenlabs.io/app/agents</a></li>
                    <li>Enable "Require authentication" in agent security settings</li>
                    <li>Add client tools (hangup_call, transfer_call, etc.) in the Tools tab</li>
                    <li>Link tools to your agent under "Dependent agents"</li>
                </ul>
                <a
                    href="/docs/contributing/references/Provider-ElevenLabs-Implementation.md"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-sm text-primary hover:underline mt-3"
                >
                    View Full Setup Guide
                    <ExternalLink className="w-3 h-3" />
                </a>
            </div>

            {/* Tools Info */}
            <div>
                <h4 className="font-semibold mb-3">Client Tools</h4>
                <p className="text-sm text-muted-foreground mb-3">
                    These tools are defined in the ElevenLabs dashboard but executed by this system.
                    Ensure tool names match exactly.
                </p>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                    {['hangup_call', 'transfer_call', 'cancel_transfer', 'leave_voicemail', 'send_email_summary', 'request_transcript'].map((tool) => (
                        <div key={tool} className="px-3 py-2 bg-muted rounded text-xs font-mono">
                            {tool}
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
};

export default ElevenLabsProviderForm;
