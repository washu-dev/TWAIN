import AgentInterface
import json

class Prompt:
    def __init__(self):
        self.agent = AgentInterface.AgentInterface()
        self.state = "USER_PROMPT"
        self.userPrompt = ""
        self.subject = ""
        self.cost = 0

    def generateJsonQuery(self, query, system = "", model = "claude-sonnet-4-6", maxTokens = 1024, ):
        jsonQuery = {
            "model": model,
            "max_tokens": maxTokens,
            "messages": [{"role":"user","content":query}],
        }
        if system != "":
            jsonQuery["system"] = system
        return jsonQuery

    # Generates prompt to create the actual data
    def primaryDataPrompt(self):
        prompt = ""
        with open("Restraints.txt","r") as file:
            prompt += file.read()
            prompt += "\n"
        match self.subject:
            case "Pymatgen":
                print("PYMATGEN CHOSEN...")
                with open("../Schema/PymatgenSchema.json","r") as schemaFile:
                    prompt += schemaFile.read()
                    prompt += "\n"
                prompt += self.userPrompt
            case "Atomic Simulation Package":
                print("Why did you go and pick that one claude")
        return prompt

    # Generates prompt to query about which model to use
    def subjectPrompt(self, userPrompt):
        prompt = ""
        with open("SubjectPrompt.txt","r") as file:
            prompt += file.read()
        prompt += "\n"
        prompt += userPrompt
        return prompt

    def choosingSubject(self):
        subjectPrompt = self.subjectPrompt(self.userPrompt)
        jsonQuery = self.generateJsonQuery(subjectPrompt, model="claude-sonnet-4-6")
        print("Choosing Subject..." + "\n")
        response = self.agent.CallAgent(jsonQuery)
        print("SUBJECT CHOSEN: " + response["content"][0]["text"])
        print("\n")
        print("FULL SUBJECT RESPONSE: " + str(response))
        self.cost += response["quotaResponse"]["TotalTokenCost"]
        _subject = response["content"][0]["text"].strip().strip("\"'").strip()
        if _subject != "Pymatgen" and _subject != "Atomic Simulation Environment":
            print("WRONG")
            print(_subject)
            self.subject = "Pymatgen"
        self.subject = _subject
        self.state = "CREATING_DATA"

    def creatingData(self):

        primaryDataPrompt = self.primaryDataPrompt()
        jsonQuery = self.generateJsonQuery(primaryDataPrompt)
        print("CREATING DATA...")
        print("THINKING...")
        print("\n")
        response = self.agent.CallAgent(jsonQuery)
        print("FULL RESPONSE:" + str(response))
        self.cost += response["quotaResponse"]["TotalTokenCost"]
        print("COST: " + str(self.cost))
        jsonData = response["content"][0]["text"].strip().strip("`").removeprefix("json")
        with open("testFiles/newFile.json", "w") as file:
            file.write(jsonData)

        exit()

    def run(self):
        while True:
            match self.state:
                case "USER_PROMPT":
                    self.userPrompt = input("ENTER YOUR PROMPT: ")
                    self.state = "CHOOSING_SUBJECT"
                case "CHOOSING_SUBJECT":
                    self.choosingSubject()
                case "CREATING_DATA":
                    self.creatingData()


if __name__ == "__main__":
    promption = Prompt()
    promption.run()
    
