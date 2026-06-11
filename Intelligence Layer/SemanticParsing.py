import AgentInterface
import PromptCompiler
import json

class Prompter:
    def __init__(self):
        self.agent = AgentInterface.AgentInterface()
        self.promptCompiler = PromptCompiler.PromptCompiler()
        self.userPrompt = ""
        self.state = "GET_PROMPT"
        self.method = ""
        self.cost = 0
        self.validSubjects = ["Pymatgen","AtomicSimulationEnvironment"]
        self.data = {}


    def nextState(self, input = ""):
        match self.state:
            case "GET_PROMPT":
                self.state = "CHOOSE_SUBJECT"
            case "CHOOSE_SUBJECT":
                print(self.method)
                print(self.method in self.validSubjects)
                if(self.method not in self.validSubjects):
                    self.state = "GET_PROMPT"
                else:
                    self.state = "SUBJECT_CONFIRMATION"
            case "SUBJECT_CONFIRMATION":
                if input.strip()[0].lower() == "y":
                    self.state = "CREATING_DATA"
                elif input.strip()[0].lower() == "n":
                    print("Please specify method in prompt")
                    self.state = "GET_PROMPT"
                else:
                    print("Invalid input. Please input only 'YES' or 'NO'")
            case "CREATING_DATA":
                self.state = "DATA_OUTPUT"
        self.switchState(self.state)

    def switchState(self, newState):
        match newState:
            case "CHOOSE_SUBJECT":
                pass
            case "DATA_PROMPT":
                pass

    def run(self):
        while True:
            match self.state:
                case "GET_PROMPT":
                    self.userPrompt = input("Please enter your prompt: ")
                    self.nextState()
                case "CHOOSE_SUBJECT":
                    prompt = self.promptCompiler.setUserPrompt(self.userPrompt).subjectPrompt()
                    self.method = self.agent.callAgent(prompt)["content"][0]["text"].strip('"')
                    print(self.method)
                    self.nextState()
                case "SUBJECT_CONFIRMATION":
                    inp = input(f"Is {self.method} the correct library to run the experiment? (Y/N): ")
                    self.nextState(inp)
                case "CREATING_DATA":
                    dataPrompt = self.promptCompiler.dataPrompt(self.method)
                    self.data = self.agent.callAgent(dataPrompt)
                    print(self.data)
                    self.nextState()
                case "DATA_OUTPUT":
                    print(self.data)
                    with open("data.json", "w") as outfile:
                        outfile.write(self.data["content"][0]["text"].strip("`").strip("json"))
                    exit()


if __name__ == "__main__":
    promption = Prompter()
    promption.run()
    
