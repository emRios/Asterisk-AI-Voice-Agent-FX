import { Book, ExternalLink, HelpCircle } from 'lucide-react';
import { ConfigSection } from '../components/ui/ConfigSection';
import { ConfigCard } from '../components/ui/ConfigCard';

const HelpPage = () => {
    return (
        <div className="space-y-6">
            <div>
                <h1 className="text-3xl font-bold tracking-tight">Help & Documentation</h1>
                <p className="text-muted-foreground mt-1">
                    Quick reference and links to comprehensive documentation.
                </p>
            </div>

            <ConfigSection title="Quick Start Guide" description="Get up and running quickly">
                <ConfigCard>
                    <div className="space-y-4">
                        <div>
                            <h4 className="font-semibold mb-2">1. Configure Providers</h4>
                            <p className="text-sm text-muted-foreground">
                                Navigate to <strong>Providers</strong> page and add your AI service providers (OpenAI, Deepgram, Google, etc.).
                                Use the "Test Connection" button to verify your API keys.
                            </p>
                        </div>
                        <div>
                            <h4 className="font-semibold mb-2">2. Set Up Pipelines</h4>
                            <p className="text-sm text-muted-foreground">
                                Go to <strong>Pipelines</strong> and configure how STT, LLM, and TTS work together.
                                Select the active pipeline in the General settings.
                            </p>
                        </div>
                        <div>
                            <h4 className="font-semibold mb-2">3. Configure Asterisk Connection</h4>
                            <p className="text-sm text-muted-foreground">
                                Ensure your Asterisk ARI credentials are correct in the <strong>Environment Variables</strong> page.
                                Test the connection in the Setup Wizard if needed.
                            </p>
                        </div>
                        <div>
                            <h4 className="font-semibold mb-2">4. Monitor System</h4>
                            <p className="text-sm text-muted-foreground">
                                Use the <strong>Dashboard</strong> to monitor system health, container status, and resource usage.
                            </p>
                        </div>
                    </div>
                </ConfigCard>
            </ConfigSection>

            <ConfigSection title="Documentation Links" description="Comprehensive guides and references">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <ConfigCard>
                        <a
                            href="https://github.com/hkjarral/Asterisk-AI-Voice-Agent/blob/main/README.md"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-start gap-3 group"
                        >
                            <Book className="w-5 h-5 text-primary mt-0.5" />
                            <div className="flex-1">
                                <h4 className="font-semibold group-hover:text-primary transition-colors">Main README</h4>
                                <p className="text-sm text-muted-foreground">Project overview, features, and quick start guide</p>
                            </div>
                            <ExternalLink className="w-4 h-4 text-muted-foreground" />
                        </a>
                    </ConfigCard>

                    <ConfigCard>
                        <a
                            href="https://github.com/hkjarral/Asterisk-AI-Voice-Agent/blob/main/docs/Configuration-Reference.md"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-start gap-3 group"
                        >
                            <Book className="w-5 h-5 text-primary mt-0.5" />
                            <div className="flex-1">
                                <h4 className="font-semibold group-hover:text-primary transition-colors">Configuration Reference</h4>
                                <p className="text-sm text-muted-foreground">Complete YAML configuration documentation</p>
                            </div>
                            <ExternalLink className="w-4 h-4 text-muted-foreground" />
                        </a>
                    </ConfigCard>

                    <ConfigCard>
                        <a
                            href="https://github.com/hkjarral/Asterisk-AI-Voice-Agent/blob/main/docs/FreePBX-Integration-Guide.md"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-start gap-3 group"
                        >
                            <Book className="w-5 h-5 text-primary mt-0.5" />
                            <div className="flex-1">
                                <h4 className="font-semibold group-hover:text-primary transition-colors">FreePBX Integration</h4>
                                <p className="text-sm text-muted-foreground">Dialplan setup and FreePBX configuration</p>
                            </div>
                            <ExternalLink className="w-4 h-4 text-muted-foreground" />
                        </a>
                    </ConfigCard>

                    <ConfigCard>
                        <a
                            href="https://github.com/hkjarral/Asterisk-AI-Voice-Agent/blob/main/docs/TOOL_CALLING_GUIDE.md"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-start gap-3 group"
                        >
                            <Book className="w-5 h-5 text-primary mt-0.5" />
                            <div className="flex-1">
                                <h4 className="font-semibold group-hover:text-primary transition-colors">Tool Calling Guide</h4>
                                <p className="text-sm text-muted-foreground">Configure AI-powered actions and integrations</p>
                            </div>
                            <ExternalLink className="w-4 h-4 text-muted-foreground" />
                        </a>
                    </ConfigCard>
                </div>
            </ConfigSection>

            <ConfigSection title="FAQ" description="Common questions and answers">
                <ConfigCard>
                    <div className="space-y-4">
                        <div>
                            <h4 className="font-semibold mb-1 flex items-center gap-2">
                                <HelpCircle className="w-4 h-4 text-primary" />
                                What's the difference between Providers and Pipelines?
                            </h4>
                            <p className="text-sm text-muted-foreground pl-6">
                                <strong>Providers</strong> are the individual AI services (OpenAI, Deepgram, etc.).
                                <strong>Pipelines</strong> combine providers into complete workflows (STT → LLM → TTS).
                            </p>
                        </div>
                        <div>
                            <h4 className="font-semibold mb-1 flex items-center gap-2">
                                <HelpCircle className="w-4 h-4 text-primary" />
                                How do I test my configuration without making a call?
                            </h4>
                            <p className="text-sm text-muted-foreground pl-6">
                                Use the "Test Connection" buttons on the Providers page and the Setup Wizard's "Test" buttons to validate API keys and connections.
                            </p>
                        </div>
                        <div>
                            <h4 className="font-semibold mb-1 flex items-center gap-2">
                                <HelpCircle className="w-4 h-4 text-primary" />
                                Are my configurations backed up?
                            </h4>
                            <p className="text-sm text-muted-foreground pl-6">
                                Yes! Every time you save, the Admin UI creates automatic timestamped backups.
                                You can also export your configuration manually for safekeeping.
                            </p>
                        </div>
                        <div>
                            <h4 className="font-semibold mb-1 flex items-center gap-2">
                                <HelpCircle className="w-4 h-4 text-primary" />
                                What should I use for production: CLI or Admin UI?
                            </h4>
                            <p className="text-sm text-muted-foreground pl-6">
                                For initial setup, use <code>install.sh</code> and the CLI tools. The Admin UI is perfect for ongoing configuration management,
                                testing, and monitoring. It's in Beta, so while stable, production-critical changes should be tested carefully.
                            </p>
                        </div>
                    </div>
                </ConfigCard>
            </ConfigSection>

        </div>
    );
};

export default HelpPage;
