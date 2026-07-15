import twain_paths


class PromptCompiler:
    # Setup
    def __init__(self):
        self.prompts = []
        self.userPrompt = ""

    # PROMPT INPUTS
    def promptFromFile(self, file):
        with open(f"{file}", "r") as file:
            self.prompts.append(file.read())
        return self

    def promptFromText(self, text):
        self.prompts.append(text)
        return self

    def setUserPrompt(self, userPrompt):
        self.userPrompt = userPrompt
        return self

    # Variable Modification
    def getPrompt(self):
        return "\n".join(self.prompts)

    def resetPrompt(self):
        self.prompts = []
        return self

    def resetUserPrompt(self):  # Technically redundant but felt like it balanced resetPrompt
        self.userPrompt = ""
        return self

    # SHORTCUTS
    def dataPrompt(self, subject):
        self.resetPrompt()
        self.promptFromFile(twain_paths.INTELLIGENCE_DIR / "Restraints.txt")
        self.promptFromFile(twain_paths.SCHEMAS_DIR / f"{subject[:1].upper()}{subject[1:].lower()}Schema.json")
        self.promptFromText(self.userPrompt)
        return self.getPrompt()

    def subjectPrompt(self):
        self.resetPrompt()
        self.promptFromFile(twain_paths.INTELLIGENCE_DIR / "SubjectPrompt.txt")
        self.promptFromText(self.userPrompt)
        return self.getPrompt()


class PromptGenerator:
    def __init__(self):
        self.pComp = PromptCompiler()

    def jsonSchemaPrompt(self, schema, query):
        prompt = "You will be generating a json file to answer the following question:"
        prompt += query
        prompt += "The json file must align exactly with the following json file"
        with open(f"{schema}", "r") as f:
            prompt += f.read()
        prompt += (
            " Choose the system representation that matches the system TYPE: a discrete, "
            "finite molecule uses `molecule` with a SMILES and kind='molecule'; a periodic "
            "solid -- a crystal, bulk metal, semiconductor, oxide, or a surface/slab of one -- "
            "uses `crystal` with kind='crystal' (or 'surface'), identifying the polymorph/phase "
            "when the composition has several (e.g. rutile vs anatase TiO2). SMILES cannot "
            "represent a periodic solid, so never invent a SMILES for a crystal. Emit only the "
            "confidence scores relevant to the chosen representation."
        )
        prompt += "Your response MUST begin with '{', the first character of a json file, and end with '}', the last character of the json file"

        return prompt

    def modifyJsonSchema(self, schema, query):
        prompt = "You will be rewriting the json schema below with the purpose of clarifying ambiguities. Your goal is to resolve any uncertainty, but do not blindly overwrite anything"
        prompt += "\n" + schema
        prompt += "+\n The following is the additional information provided by the user to resolve ambiguities"
        prompt += query
        prompt += "Your response MUST begin with '{', the first character of a json file, and end with '}', the last character of the json file"
        return prompt

    def clarificationPrompt(self, intent_spec):
        prompt = (
            "You are to generate a list of questions for the following schema file to resolve "
            "the ambiguities. Based on the following json file, return an ordered list of "
            "specific questions whose answers will remove any uncertainty. Ask ONLY about "
            "fields relevant to the chosen system representation: for a periodic solid "
            "(kind='crystal' or 'surface') ask about the polymorph/phase or the structure "
            "source (e.g. which TiO2 polymorph -- rutile, anatase, or brookite -- or a "
            "Materials Project id / CIF), and NEVER ask for the SMILES of a solid. For a "
            "discrete molecule (kind='molecule') ask about its identity / SMILES. Do not ask "
            "about the confidence scores themselves."
        )
        prompt += intent_spec
        return prompt

    def goalGraphPrompt(self, schema, intent_spec, source_intent_id):
        prompt = (
            "You are decomposing a computational-chemistry research request into an "
            "executable goal graph: a directed acyclic graph (DAG) of sub-goals connected "
            "by dependency edges. Break the work into the smallest set of ordered sub-goals "
            "the request actually needs (e.g. discover a method, prepare inputs, run it, "
            "validate against the acceptance metrics, review). Tailor the goals to THIS "
            "request rather than emitting a fixed template. Every edge's source and target "
            "MUST reference goal ids you define, and the graph MUST be acyclic. Fold the "
            "request's acceptance metrics into the validation goal's acceptance_criteria. "
            f"Use '{source_intent_id}' as metadata.source_intent_id. "
            "The research request (IntentSpec) to decompose is:\n"
        )
        prompt += intent_spec
        prompt += "\nThe json file must align exactly with the following json schema:\n"
        with open(f"{schema}", "r") as f:
            prompt += f.read()
        prompt += "Your response MUST begin with '{', the first character of a json file, and end with '}', the last character of the json file"
        return prompt


