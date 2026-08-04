"""Prompt assembly for the control plane.

:class:`PromptGenerator` builds the specific stage prompts the state machine
sends to the LLM (schema-conformant JSON, clarification questions, goal-graph
decomposition).
"""


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

    def clarification_prompt(self, intent_spec, uncertain_fields=None):
        """Prompt for the FEWEST, most concise clarification questions.

        ``uncertain_fields`` (optional) is the list of IntentSpec fields whose
        confidence is below threshold. When supplied, the model is told to ask
        ONLY about those and to combine them, so CLARIFY stays a quick one- or
        two-question exchange instead of re-interrogating fields intake already
        resolved. When empty, it asks the single most essential question. If
        nothing genuinely needs clarifying the model replies "No questions.".
        """
        fields = [f for f in (uncertain_fields or []) if f]
        if fields:
            focus = (
                "Ask ONLY about these unresolved fields, most important first: "
                + ", ".join(fields) + ". "
            )
            cap = max(1, min(len(fields), 3))
        else:
            focus = (
                "No single field is flagged as uncertain; ask only the one question "
                "most essential to proceed, or none if the request is already clear. "
            )
            cap = 1
        prompt = (
            "You help a computational-chemistry assistant fill the smallest gaps in a "
            "parsed research request (the JSON IntentSpec below). "
            + focus
            + f"Ask the FEWEST questions possible: merge related gaps into a single "
            f"question and ask at most {cap}. Each question must be one short, plain-language "
            "sentence a researcher can answer in a few words -- no numbering, no preamble, "
            "no explanations, and do not restate the request back to them. Respect the "
            "system representation: for a periodic solid (kind='crystal' or 'surface') ask "
            "about the polymorph/phase or a structure source (a specific crystalline form, a "
            "Materials Project id, or a CIF) and NEVER ask for a SMILES; for a discrete "
            "molecule (kind='molecule') ask about its identity or SMILES. Never ask about "
            "the confidence scores themselves. If nothing genuinely needs clarifying, reply "
            "with exactly: No questions.\n\nIntentSpec:\n"
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


