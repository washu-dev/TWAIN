"""Prompt assembly for the control plane.

:class:`PromptCompiler` is a small fluent builder for stacking prompt fragments
(from files or literal text) into one string. :class:`PromptGenerator` builds the
specific stage prompts the state machine sends to the LLM (schema-conformant
JSON, clarification questions, goal-graph decomposition).
"""
import twain_paths


class PromptCompiler:
    # Setup
    def __init__(self):
        self.prompts = []
        self.user_prompt = ""

    # PROMPT INPUTS
    def prompt_from_file(self, file):
        with open(f"{file}", "r") as file:
            self.prompts.append(file.read())
        return self

    def prompt_from_text(self, text):
        self.prompts.append(text)
        return self

    def set_user_prompt(self, user_prompt):
        self.user_prompt = user_prompt
        return self

    # Variable Modification
    def get_prompt(self):
        return "\n".join(self.prompts)

    def reset_prompt(self):
        self.prompts = []
        return self

    # SHORTCUTS
    def data_prompt(self, subject):
        self.reset_prompt()
        self.prompt_from_file(twain_paths.INTELLIGENCE_DIR / "Restraints.txt")
        self.prompt_from_file(twain_paths.SCHEMAS_DIR / f"{subject[:1].upper()}{subject[1:].lower()}Schema.json")
        self.prompt_from_text(self.user_prompt)
        return self.get_prompt()

    def subject_prompt(self):
        self.reset_prompt()
        self.prompt_from_file(twain_paths.INTELLIGENCE_DIR / "SubjectPrompt.txt")
        self.prompt_from_text(self.user_prompt)
        return self.get_prompt()


class PromptGenerator:
    def json_schema_prompt(self, schema, query):
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
            "when the composition has several distinct crystalline forms. SMILES cannot "
            "represent a periodic solid, so never invent a SMILES for a crystal. Emit only the "
            "confidence scores relevant to the chosen representation."
        )
        prompt += "Your response MUST begin with '{', the first character of a json file, and end with '}', the last character of the json file"

        return prompt

    def modify_json_schema(self, schema, query):
        prompt = "You will be rewriting the json schema below with the purpose of clarifying ambiguities. Your goal is to resolve any uncertainty, but do not blindly overwrite anything"
        prompt += "\n" + schema
        prompt += "+\n The following is the additional information provided by the user to resolve ambiguities"
        prompt += query
        prompt += "Your response MUST begin with '{', the first character of a json file, and end with '}', the last character of the json file"
        return prompt

    def clarification_prompt(self, intent_spec):
        prompt = (
            "You are to generate a list of questions for the following schema file to resolve "
            "the ambiguities. Based on the following json file, return an ordered list of "
            "specific questions whose answers will remove any uncertainty. Ask ONLY about "
            "fields relevant to the chosen system representation: for a periodic solid "
            "(kind='crystal' or 'surface') ask about the polymorph/phase or the structure "
            "source (e.g. which specific crystalline form, or a Materials Project id or "
            "CIF), and NEVER ask for the SMILES of a solid. For a "
            "discrete molecule (kind='molecule') ask about its identity / SMILES. Do not ask "
            "about the confidence scores themselves."
        )
        prompt += intent_spec
        return prompt

    def goal_graph_prompt(self, schema, intent_spec, source_intent_id):
        prompt = (
            "You are decomposing a computational-chemistry research request into an "
            "executable goal graph: a directed acyclic graph (DAG) of sub-goals connected "
            "by dependency edges. Break the work into the smallest set of ordered sub-goals "
            "the request actually needs (e.g. discover a method, prepare inputs, run it, "
            "validate against the acceptance metrics, review). Tailor the goals to THIS "
            "request rather than emitting a fixed template. When the property requires it, "
            "include explicit sub-goals for validating the built structure (correct "
            "stoichiometry and physically reasonable geometry), relaxing the geometry to "
            "equilibrium, and converging the numerical settings before the property is "
            "computed -- a rigorous result depends on these, not just on running the tool "
            "once. Every edge's source and target "
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


