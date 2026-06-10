import AgentInterface

class Prompt:
    def __init__(self):
        pass

    def generateJsonQuery(self, query, system = "", model = "claude-sonnet-4-6", maxTokens = 1024, ):
        jsonQuery = {
            "model": model,
            "max_tokens": maxTokens,
            "messages": [{"role":"user","content":query}],
        }
        if system != "":
            jsonQuery["system"] = system
        return jsonQuery
    
    def generatePrompt(self, userPrompt):
        prompt = ""
        prompt += userPrompt
        prompt += "\n"
        with open("Restraints.txt","r") as file:
            prompt += file.read()
            prompt += "\n"
        with open("SchemaInstructions.txt","r") as file:
            prompt += file.read()
            prompt += "\n"
        with open("../Schema/PymatgenSchema.json","r") as schemaFile:
            prompt += schemaFile.read()
            prompt += "\n"
        return prompt

    def prompt(self, prompt):
        print(self.generateJsonQuery(self.generatePrompt(prompt)))
        agent = AgentInterface.AgentInterface()
        response = agent.CallAgent(self.generateJsonQuery(self.generatePrompt(prompt)))
        print(response)
        print(response["content"][0]["text"])  # Output
    def Test(self):
        self.prompt("Do MPRelaxSet in Pymatgen with graphite")

if __name__ == "__main__":
    promption = Prompt()
    promption.Test()
    
